"""Offline ARM contracts. Run: python3 -B tests/test_infrastructure_contracts.py -v

Requires an already-installed Azure CLI and Bicep compiler, not Azure credentials.
All root templates compile to stdout once. Sanitized parameter-example copies
live briefly beneath tests/ and are removed, including on compilation failure.
No deployments, secret resolution, external module restores, or live agent tests
are performed. The small expression reader below intentionally does not emulate
ARM deployments: runtime references, resource IDs, GUIDs and lambdas are checked
structurally, rather than assigned invented runtime behavior.
"""

import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass


ROOT = Path(__file__).resolve().parents[1]
NONSECRET = "TEST-ONLY-NOT-A-SECRET"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
OTHER_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
BLOB_ZONE = "privatelink.blob.core.windows.net"
CORE_ZONES = {
    "privatelink.cognitiveservices.azure.com",
    "privatelink.openai.azure.com",
    "privatelink.services.ai.azure.com",
    BLOB_ZONE,
    "privatelink.search.windows.net",
    "privatelink.documents.azure.com",
}
MONITOR_ZONES = {
    "privatelink.monitor.azure.com",
    "privatelink.oms.opinsights.azure.com",
    "privatelink.ods.opinsights.azure.com",
    "privatelink.agentsvc.azure-automation.net",
    BLOB_ZONE,
}
VAULT_ZONE = "privatelink.vaultcore.azure.net"
ACR_ZONE = "privatelink.azurecr.io"
STORAGE_OWNER = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
TELEMETRY_ROLES = {
    "73c42c96-874c-492b-b04d-ab87d138a893",
    "dbc9c667-e97f-4491-aee6-90b9cf960190",
}


def check_compiler_result(result, path):
    if result.returncode:
        raise AssertionError(f"{path}: compilation failed\n{result.stderr}")
    unexpected = [
        line for line in result.stderr.splitlines()
        if line.strip() and not re.fullmatch(
            r'WARNING: A new Bicep release is available: \S+\. '
            r'Upgrade now by running "az bicep upgrade"\.', line.strip()
        )
    ]
    if unexpected:
        raise AssertionError(f"{path}: compiler diagnostics:\n" + "\n".join(unexpected))
    return json.loads(result.stdout)


def compiler_environment():
    if not shutil.which("az"):
        raise RuntimeError("Install Azure CLI and Bicep before running these offline tests.")
    config = Path(os.environ.get("AZURE_CONFIG_DIR", Path.home() / ".azure"))
    on_path = shutil.which("bicep") is not None
    if not on_path and not (config / "bin" / "bicep").is_file():
        raise RuntimeError("Bicep is not installed; offline tests will not download it.")
    return dict(
        os.environ,
        AZURE_BICEP_CHECK_VERSION="false",
        AZURE_BICEP_USE_BINARY_FROM_PATH=str(on_path).lower(),
        AZURE_CORE_COLLECT_TELEMETRY="false",
        AZURE_CORE_NO_COLOR="true",
    )


def compile_file(path, environment, command="build"):
    result = subprocess.run(
        ["az", "bicep", command, "--file", str(path), "--stdout", "--no-restore"],
        cwd=ROOT, env=environment, text=True, capture_output=True, timeout=120,
        check=False,
    )
    return check_compiler_result(result, path.name)


def sanitized_example(text, template, destination):
    # Bicep rejects absolute `using` paths (BCP051), so relativize the resolved
    # absolute target from the scratch copy, not from the caller's current cwd.
    using_path = os.path.relpath(template.resolve(), destination.parent)
    text, count = re.subn(
        r"(?m)^using\s+'[^']+'\s*$",
        lambda _: f"using '{using_path.replace(chr(39), chr(39) * 2)}'",
        text,
    )
    if count != 1:
        raise AssertionError("Expected exactly one example using declaration.")
    text = re.sub(
        r"\baz\.getSecret\s*\([^)]*\)", f"'{NONSECRET}'", text, flags=re.DOTALL
    )
    if re.search(r"\baz\s*\.", text):
        raise AssertionError("Refusing to compile an unsanitized Azure parameter function.")
    return text


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple


@dataclass(frozen=True)
class Access:
    value: object
    key: object


def expression(value):
    """Parse only ARM's function-call, literal and property/index syntax."""
    if not isinstance(value, str) or not value.startswith("["):
        return value
    if not value.endswith("]"):
        raise ValueError(f"Unterminated ARM expression: {value}")
    source = value[1:-1]
    token = re.compile(r"\s*('(?:[^']|'')*'|-?\d+|[A-Za-z_]\w*|[(),.\[\]])")
    tokens = []
    position = 0
    while position < len(source):
        match = token.match(source, position)
        if not match:
            raise ValueError(f"Unsupported ARM syntax: {source[position:]}")
        tokens.append(match[1])
        position = match.end()
    index = 0

    def consume(expected=None):
        nonlocal index
        item = tokens[index]
        if expected is not None and item != expected:
            raise ValueError(f"Expected {expected!r}, got {item!r}")
        index += 1
        return item

    def parse():
        item = consume()
        if item.startswith("'"):
            result = item[1:-1].replace("''", "'")
        elif re.fullmatch(r"-?\d+", item):
            result = int(item)
        else:
            consume("(")
            args = []
            if tokens[index] != ")":
                args.append(parse())
                while tokens[index] == ",":
                    consume(",")
                    args.append(parse())
            consume(")")
            result = Call(item, tuple(args))
        while index < len(tokens) and tokens[index] in (".", "["):
            if consume() == ".":
                result = Access(result, consume())
            else:
                result = Access(result, parse())
                consume("]")
        return result

    result = parse()
    if index != len(tokens):
        raise ValueError(f"Unconsumed ARM syntax: {tokens[index:]}")
    return result


def calls(value, name):
    if isinstance(value, Call):
        if value.name.lower() == name.lower():
            yield value
        for arg in value.args:
            yield from calls(arg, name)
    elif isinstance(value, Access):
        yield from calls(value.value, name)
        yield from calls(value.key, name)


class ArmSubset:
    """Evaluate pure configuration expressions only; fail closed otherwise."""

    def __init__(self, template, parameters=None, copy_index=0):
        self.template = template
        self.parameters = parameters or {}
        self.copy_index = copy_index

    def __call__(self, value):
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        return self.evaluate(expression(value))

    def variable(self, name):
        return self(self.template["variables"][name])

    def evaluate(self, node):
        if isinstance(node, Access):
            return self.evaluate(node.value)[self.evaluate(node.key)]
        if not isinstance(node, Call):
            return node
        name, args = node.name.lower(), node.args
        if name == "if":
            return self.evaluate(args[1] if self.evaluate(args[0]) else args[2])
        if name in ("and", "or"):
            values = (bool(self.evaluate(arg)) for arg in args)
            return all(values) if name == "and" else any(values)
        values = [self.evaluate(arg) for arg in args]
        if name == "parameters":
            key = values[0]
            if key in self.parameters:
                return self.parameters[key]
            return self(self.template["parameters"][key]["defaultValue"])
        if name == "variables":
            return self.variable(values[0])
        if name == "union":
            if isinstance(values[0], dict):
                return dict(item for value in values for item in value.items())
            return list(dict.fromkeys(item for value in values for item in value))
        operations = {
            "true": lambda: True,
            "false": lambda: False,
            "null": lambda: None,
            "not": lambda value: not value,
            "equals": lambda left, right: left == right,
            "empty": lambda value: not value,
            "contains": lambda value, key: key in value,
            "trim": str.strip,
            "tolower": str.lower,
            "split": str.split,
            "last": lambda value: value[-1],
            "length": len,
            "format": lambda fmt, *items: fmt.format(*items),
            "replace": str.replace,
            "createarray": lambda *items: list(items),
            "createobject": lambda *items: dict(zip(items[::2], items[1::2])),
            "copyindex": lambda *unused: self.copy_index,
            "subscription": lambda: {"subscriptionId": SUBSCRIPTION},
            "resourcegroup": lambda: {
                "name": "stack-rg", "location": "australiaeast",
                "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/stack-rg",
            },
            "environment": lambda: {"suffixes": {"storage": "core.windows.net"}},
        }
        if name not in operations:
            raise ValueError(f"{node.name} is outside the contract evaluator's subset")
        return operations[name](*values)


def resources(template, resource_type):
    return [
        resource for resource in template.get("resources", [])
        if resource["type"].lower() == resource_type.lower()
    ]


def walk_resources(template):
    for resource in template.get("resources", []):
        yield resource
        nested = resource.get("properties", {}).get("template")
        if nested:
            yield from walk_resources(nested)


def module_parameters(module):
    return module["properties"]["parameters"]


def phase_module(template, phase):
    matches = [
        resource for resource in resources(template, "Microsoft.Resources/deployments")
        if module_parameters(resource).get("phase") == {"value": phase}
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {phase} module, got {len(matches)}")
    return matches[0]


class InfrastructureContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = compiler_environment()
        cls.templates = {}
        failures = []
        for path in sorted(ROOT.glob("*.bicep")):
            try:
                cls.templates[path.stem] = compile_file(path, cls.environment)
            except (AssertionError, subprocess.TimeoutExpired) as error:
                failures.append(str(error))
        if failures:
            raise AssertionError("\n\n".join(failures))
        cls.main = cls.templates["main"]

    def one(self, template, resource_type):
        found = resources(template, resource_type)
        self.assertEqual(len(found), 1, resource_type)
        return found[0]

    def module(self, name):
        return next(
            resource for resource in resources(self.main, "Microsoft.Resources/deployments")
            if resource["name"] == name
        )

    def vault_module(self):
        return next(
            resource for resource in resources(self.main, "Microsoft.Resources/deployments")
            if "createVault" in module_parameters(resource)
        )

    def assert_expression(self, actual, expected):
        self.assertEqual(expression(actual), expression(expected))

    def assert_module_output(self, actual, module, output):
        node = expression(actual)
        for key in ("value", output, "outputs"):
            self.assertIsInstance(node, Access)
            self.assertEqual(node.key, key)
            node = node.value
        self.assertIsInstance(node, Call)
        self.assertEqual(node.name, "reference")
        resource_id = node.args[0]
        self.assertIsInstance(resource_id, Call)
        self.assertIn(resource_id.name, ("resourceId", "extensionResourceId"))
        self.assertEqual(resource_id.args[-2], "Microsoft.Resources/deployments")
        self.assertEqual(resource_id.args[-1], expression(module["name"]))

    def test_all_root_templates_compile_without_warnings(self):
        self.assertEqual(
            set(self.templates), {path.stem for path in ROOT.glob("*.bicep")}
        )
        self.assertTrue({
            "main", "add-project", "diagnostics", "dns", "foundry",
            "foundry-roles", "foundry-private-endpoints", "key-vault", "vm",
        }.issubset(self.templates))
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn("deploymenttemplate.json", template["$schema"].lower())
                self.assertTrue(template["resources"])

    def test_main_defaults_and_model_wiring_are_preserved(self):
        defaults = {
            "location": "australiaeast", "prefix": "aifoundrybicep",
            "fwProvision": True, "fwSku": "Basic", "bastionProvision": True,
            "vmDeploy": True, "enableContainerRegistry": True,
            "enableAgentTracing": True, "enableKeyVault": True,
            "existingKeyVaultResourceId": "", "keyVaultName": "",
            "vmSize": "Standard_D8s_v5", "modelName": "gpt-4o-mini",
            "modelFormat": "OpenAI", "modelVersion": "2024-07-18",
            "modelSkuName": "GlobalStandard", "modelCapacity": 30,
        }
        for key, expected in defaults.items():
            with self.subTest(parameter=key):
                self.assertEqual(self.main["parameters"][key]["defaultValue"], expected)
        for key in ("modelName", "modelFormat", "modelVersion", "modelSkuName", "modelCapacity"):
            self.assert_expression(
                module_parameters(self.module("foundry"))[key]["value"],
                f"[parameters('{key}')]",
            )
        deployment = self.one(
            self.templates["foundry"], "Microsoft.CognitiveServices/accounts/deployments"
        )
        for field, parameter in (("name", "modelName"), ("format", "modelFormat"), ("version", "modelVersion")):
            self.assert_expression(
                deployment["properties"]["model"][field], f"[parameters('{parameter}')]"
            )
        self.assert_expression(deployment["sku"]["name"], "[parameters('modelSkuName')]")
        self.assert_expression(deployment["sku"]["capacity"], "[parameters('modelCapacity')]")

    def test_naming_preserves_existing_suffix_and_resource_patterns(self):
        self.assert_expression(
            self.main["variables"]["suffix"],
            "[if(empty(parameters('randomSuffix')), "
            "take(uniqueString(subscription().subscriptionId, parameters('resourceGroupName')), 4), "
            "parameters('randomSuffix'))]",
        )
        evaluator = ArmSubset(self.main, {"randomSuffix": "AB12"})
        for key, expected in {
            "acrName": "acrab12", "aiServicesName": "aiservicesAB12",
            "aiSearchName": "aisearchAB12", "cosmosDBName": "cosmosdbAB12",
            "azureStorageName": "foundrystgab12",
        }.items():
            self.assertEqual(evaluator.variable(key), expected)

    def test_feature_modules_follow_flags_on_and_off(self):
        for flag, module in (
            ("enableKeyVault", self.vault_module()),
            ("enableContainerRegistry", self.module("acr")),
            ("enableAgentTracing", phase_module(self.main, "telemetry")),
        ):
            for enabled in (False, True):
                with self.subTest(flag=flag, enabled=enabled):
                    self.assertEqual(
                        ArmSubset(self.main, {flag: enabled})(module["condition"]), enabled
                    )
        self.assert_expression(
            module_parameters(self.module("diagnostics"))["enableAgentTracing"]["value"],
            "[parameters('enableAgentTracing')]",
        )
        for flag, module_name, outputs in (
            ("fwProvision", "firewall", ("firewallPrivateIp",)),
            ("bastionProvision", "bastion", ("bastionName",)),
            ("vmDeploy", "vm", ("vmPrivateIp",)),
            ("enableContainerRegistry", "acr", ("acrId", "acrLoginServer")),
        ):
            for enabled in (False, True):
                evaluator = ArmSubset(self.main, {flag: enabled})
                self.assertEqual(evaluator(self.module(module_name)["condition"]), enabled)
            for output in outputs:
                value = self.main["outputs"][output]["value"]
                self.assertEqual(ArmSubset(self.main, {flag: False})(value), "")
                self.assert_module_output(expression(value).args[1], self.module(module_name), output)

    def test_dns_merges_empty_partial_and_duplicate_overrides_for_every_flag_combination(self):
        custom = "privatelink.example.test"
        overrides = ([], [custom], [BLOB_ZONE, custom, BLOB_ZONE])
        for tracing, vault, acr in itertools.product((False, True), repeat=3):
            for override in overrides:
                with self.subTest(tracing=tracing, vault=vault, acr=acr, override=override):
                    evaluator = ArmSubset(self.main, {
                        "enableAgentTracing": tracing, "enableKeyVault": vault,
                        "enableContainerRegistry": acr, "privateDnsZones": override,
                    })
                    actual = evaluator(
                        module_parameters(self.module("dns"))["privateDnsZones"]["value"]
                    )
                    expected = CORE_ZONES | set(override)
                    if tracing:
                        expected |= MONITOR_ZONES
                    if vault:
                        expected.add(VAULT_ZONE)
                    if acr:
                        expected.add(ACR_ZONE)
                    self.assertEqual(set(actual), expected)
                    self.assertEqual(len(actual), len(expected))

    def test_central_dns_owns_zones_and_all_three_vnet_links_once(self):
        dns = self.templates["dns"]
        zone = self.one(dns, "Microsoft.Network/privateDnsZones")
        links = resources(dns, "Microsoft.Network/privateDnsZones/virtualNetworkLinks")
        self.assertEqual(len(links), 3)
        zones = ArmSubset(self.main, {"privateDnsZones": []}).variable("effectivePrivateDnsZones")
        names = set()
        for index, name in enumerate(zones):
            evaluator = ArmSubset(dns, {
                "privateDnsZones": zones, "prefix": "contract", "randomSuffix": "abcd",
                "hubVnetId": "/vnets/hub", "aiappVnetId": "/vnets/aiapp",
                "vmVnetId": "/vnets/vm",
            }, index)
            self.assertEqual(evaluator(zone["copy"]["count"]), len(zones))
            self.assertEqual(evaluator(zone["name"]), name)
            actual_vnets = set()
            for link in links:
                self.assertEqual(evaluator(link["copy"]["count"]), len(zones))
                link_name = evaluator(link["name"])
                self.assertTrue(link_name.startswith(name + "/"))
                self.assertNotIn(link_name, names)
                names.add(link_name)
                self.assertIs(link["properties"]["registrationEnabled"], False)
                actual_vnets.add(evaluator(link["properties"]["virtualNetwork"]["id"]))
            self.assertEqual(actual_vnets, {"/vnets/hub", "/vnets/aiapp", "/vnets/vm"})
        deployed = list(walk_resources(self.main))
        self.assertEqual(sum(r["type"].lower() == zone["type"].lower() for r in deployed), 1)
        self.assertEqual(sum(
            r["type"].lower() == "microsoft.network/privatednszones/virtualnetworklinks"
            for r in deployed
        ), 3)
        for key in ("hubVnetId", "aiappVnetId", "vmVnetId"):
            self.assert_module_output(
                module_parameters(self.module("dns"))[key]["value"],
                self.module("networking"), key,
            )
        self.assert_expression(
            dns["outputs"]["zoneIds"]["value"],
            "[toObject(parameters('privateDnsZones'), lambda('zoneName', "
            "lambdaVariables('zoneName')), lambda('zoneName', "
            "resourceId('Microsoft.Network/privateDnsZones', lambdaVariables('zoneName'))))]",
        )

    def test_all_five_monitor_zone_ids_are_mapped_from_central_dns(self):
        evaluator = ArmSubset(self.main)
        zones = evaluator.variable("monitorDnsZones")
        self.assertEqual(set(zones), MONITOR_ZONES)
        self.assertEqual(len(zones), 5)
        mapping = module_parameters(self.module("diagnostics"))["monitorDnsZoneIds"]
        self.assertEqual(ArmSubset(self.main, {"enableAgentTracing": False})(mapping), {"value": []})
        maps = list(calls(expression(mapping), "map"))
        self.assertEqual(len(maps), 1)
        self.assertEqual(maps[0].args[0], expression("[variables('monitorDnsZones')]"))
        callback = maps[0].args[1]
        self.assertEqual(callback.name, "lambda")
        lookup = callback.args[1]
        self.assertIsInstance(lookup, Access)
        self.assertEqual(lookup.key, Call("lambdaVariables", (callback.args[0],)))
        self.assert_module_output(lookup.value, self.module("dns"), "zoneIds")
        blob_lookup = module_parameters(self.module("foundryPe"))["dnsZoneIds"]["value"]
        objects = list(calls(expression(blob_lookup), "createObject"))
        blob = next(dict(zip(obj.args[::2], obj.args[1::2]))["storageBlob"]
                    for obj in objects if "storageBlob" in obj.args[::2])
        self.assertEqual(blob.key, expression("[variables('storageBlobZone')]"))
        self.assert_module_output(blob.value, self.module("dns"), "zoneIds")

    def test_monitor_dns_group_consumes_five_ids_without_recreating_zones(self):
        diagnostics = self.templates["diagnostics"]
        group = self.one(diagnostics, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups")
        loop, = group["properties"]["copy"]
        self.assertEqual(loop["name"], "privateDnsZoneConfigs")
        zone_ids = [
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/stack-rg/"
            f"providers/Microsoft.Network/privateDnsZones/{zone}"
            for zone in sorted(MONITOR_ZONES)
        ]
        configs = []
        for index in range(5):
            evaluator = ArmSubset(diagnostics, {"monitorDnsZoneIds": zone_ids}, index)
            self.assertEqual(evaluator(loop["count"]), 5)
            configs.append(evaluator(loop["input"]))
        self.assertEqual([item["properties"]["privateDnsZoneId"] for item in configs], zone_ids)
        self.assertEqual(len({item["name"] for item in configs}), 5)
        self.assertEqual(
            {item["name"] for item in configs}, {zone.replace(".", "-") for zone in MONITOR_ZONES}
        )
        for template_name in ("diagnostics", "foundry-private-endpoints", "container-registry", "key-vault"):
            self.assertFalse(resources(self.templates[template_name], "Microsoft.Network/privateDnsZones"))
            self.assertFalse(resources(
                self.templates[template_name], "Microsoft.Network/privateDnsZones/virtualNetworkLinks"
            ))

    def test_tracing_resources_are_optional_but_shared_workspace_remains(self):
        diagnostics = self.templates["diagnostics"]
        workspace = self.one(diagnostics, "Microsoft.OperationalInsights/workspaces")
        storage = self.one(diagnostics, "Microsoft.Storage/storageAccounts")
        self.assertNotIn("condition", workspace)
        self.assertNotIn("condition", storage)
        optional = [
            resource for resource in diagnostics["resources"]
            if resource not in (workspace, storage)
            and resource["type"] != "Microsoft.Resources/deployments"
        ]
        self.assertEqual(len(optional), 7)
        for enabled in (False, True):
            evaluator = ArmSubset(diagnostics, {"enableAgentTracing": enabled})
            for resource in optional:
                self.assertEqual(evaluator(resource["condition"]), enabled)
            self.assertEqual(
                evaluator(workspace["properties"]["publicNetworkAccessForIngestion"]),
                "Disabled" if enabled else "Enabled",
            )
        disabled = ArmSubset(diagnostics, {"enableAgentTracing": False})
        for output in ("appInsightsId", "appInsightsName", "appInsightsAppId", "amplsId"):
            self.assertEqual(disabled(diagnostics["outputs"][output]["value"]), "")
            self.assert_module_output(
                self.main["outputs"][output]["value"], self.module("diagnostics"), output
            )

    def test_tracing_ingestion_is_private_and_queries_remain_authorized_public(self):
        diagnostics = self.templates["diagnostics"]
        workspace = self.one(diagnostics, "Microsoft.OperationalInsights/workspaces")
        app = self.one(diagnostics, "Microsoft.Insights/components")
        scope = self.one(diagnostics, "Microsoft.Insights/privateLinkScopes")
        self.assertEqual(app["properties"]["publicNetworkAccessForIngestion"], "Disabled")
        for resource in (workspace, app):
            self.assertEqual(resource["properties"]["publicNetworkAccessForQuery"], "Enabled")
        self.assertIs(workspace["properties"]["features"]["enableLogAccessUsingOnlyResourcePermissions"], True)
        self.assertEqual(scope["properties"]["accessModeSettings"], {
            "ingestionAccessMode": "PrivateOnly", "queryAccessMode": "Open",
        })
        self.assertEqual(app["properties"]["WorkspaceResourceId"], diagnostics["outputs"]["lawId"]["value"])
        linked = resources(diagnostics, "Microsoft.Insights/privateLinkScopes/scopedResources")
        self.assertEqual({r["properties"]["linkedResourceId"] for r in linked}, {
            app["properties"]["WorkspaceResourceId"],
            self.one(diagnostics, "Microsoft.CognitiveServices/accounts/connections")["properties"]["target"],
        })
        endpoint = self.one(diagnostics, "Microsoft.Network/privateEndpoints")
        connection, = endpoint["properties"]["privateLinkServiceConnections"]
        self.assertEqual(connection["properties"]["groupIds"], ["azuremonitor"])
        self.assert_expression(endpoint["properties"]["subnet"]["id"], "[parameters('peSubnetId')]")
        scoped_dependencies = [
            expression(dependency) for dependency in endpoint["dependsOn"]
            if expression(dependency).args[0].lower()
            == "microsoft.insights/privatelinkscopes/scopedresources"
        ]
        self.assertEqual({dependency.args[-1] for dependency in scoped_dependencies}, {"appinsights", "law"})

    def test_account_shared_appinsights_connection_targets_the_shared_component(self):
        diagnostics = self.templates["diagnostics"]
        connection = self.one(diagnostics, "Microsoft.CognitiveServices/accounts/connections")
        properties = connection["properties"]
        self.assertEqual(properties["category"], "AppInsights")
        self.assertEqual(properties["authType"], "ApiKey")
        self.assertIs(properties["isSharedToAll"], True)
        self.assertEqual(properties["metadata"]["ResourceId"], properties["target"])
        self.assert_expression(
            properties["target"],
            "[resourceId('Microsoft.Insights/components', "
            "format('{0}-appi-{1}', parameters('prefix'), parameters('randomSuffix')))]",
        )
        key = expression(properties["credentials"]["key"])
        self.assertEqual(key.key, "ConnectionString")
        self.assertEqual(key.value.name, "reference")
        self.assertEqual(key.value.args[0], expression(properties["target"]))
        self.assertEqual(
            ArmSubset(diagnostics, {"aiAccountName": "account"})(
                connection["name"]
            ), "account/account-appinsights",
        )
        self.assert_module_output(
            module_parameters(self.module("diagnostics"))["aiAccountName"]["value"],
            self.module("foundry"), "accountName",
        )

    def test_new_and_existing_vault_paths_select_correct_scope_without_mutation(self):
        module = self.vault_module()
        existing = (
            f"/subscriptions/{OTHER_SUBSCRIPTION}/resourceGroups/shared-vault-rg"
            "/providers/Microsoft.KeyVault/vaults/shared-vault"
        )
        for enabled, vault_id, expected_create, subscription, group, name in (
            (True, "", True, SUBSCRIPTION, "stack-rg", "new-vault"),
            (True, " \t ", True, SUBSCRIPTION, "stack-rg", "new-vault"),
            (True, f" \t{existing}\n", False, OTHER_SUBSCRIPTION, "shared-vault-rg", "shared-vault"),
            (False, existing, True, SUBSCRIPTION, "stack-rg", "new-vault"),
        ):
            with self.subTest(enabled=enabled, vault_id=vault_id):
                evaluator = ArmSubset(self.main, {
                    "enableKeyVault": enabled, "existingKeyVaultResourceId": vault_id,
                    "resourceGroupName": "stack-rg", "keyVaultName": " new-vault ",
                })
                self.assertEqual(evaluator(module["condition"]), enabled)
                self.assertEqual(evaluator(module["subscriptionId"]), subscription)
                self.assertEqual(evaluator(module["resourceGroup"]), group)
                params = module_parameters(module)
                self.assertEqual(evaluator(params["createVault"]["value"]), expected_create)
                self.assertEqual(evaluator(params["keyVaultName"]["value"]), name)
        self.assert_expression(
            self.main["variables"]["resolvedKeyVaultName"],
            "[if(variables('useExistingKeyVault'), last(variables('existingKeyVaultParts')), "
            "if(empty(trim(parameters('keyVaultName'))), format('kv-{0}', "
            "uniqueString(subscription().subscriptionId, parameters('resourceGroupName'))), "
            "trim(parameters('keyVaultName'))))]",
        )

    def test_vault_secrets_user_uses_account_not_project_identity(self):
        vault = self.templates["key-vault"]
        resource = self.one(vault, "Microsoft.KeyVault/vaults")
        role = self.one(vault, "Microsoft.Authorization/roleAssignments")
        self.assertEqual(len(vault["resources"]), 2)
        for create in (False, True):
            self.assertEqual(ArmSubset(vault, {"createVault": create})(resource["condition"]), create)
        self.assertNotIn("condition", role)
        self.assertEqual(role["scope"], vault["outputs"]["keyVaultResourceId"]["value"])
        self.assert_expression(role["properties"]["principalId"], "[parameters('accountPrincipalId')]")
        self.assertEqual(role["properties"]["principalType"], "ServicePrincipal")
        self.assert_expression(role["properties"]["roleDefinitionId"], "[variables('secretsUserRoleId')]")
        self.assert_expression(
            vault["variables"]["secretsUserRoleId"],
            "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
            "'4633458b-17de-408a-b874-0445c86b69e6')]",
        )
        self.assert_module_output(
            module_parameters(self.vault_module())["accountPrincipalId"]["value"],
            self.module("foundry"), "accountPrincipalId",
        )
        self.assertIs(resource["properties"]["enableRbacAuthorization"], True)
        self.assertEqual(resource["properties"]["accessPolicies"], [])
        self.assertEqual(resource["properties"]["networkAcls"]["defaultAction"], "Deny")
        self.assertEqual(resource["properties"]["networkAcls"]["bypass"], "AzureServices")

    def test_vault_private_endpoint_stays_in_stack_scope_and_uses_full_id(self):
        module = self.module("foundryPe")
        self.assert_expression(module["resourceGroup"], "[parameters('resourceGroupName')]")
        self.assertNotIn("subscriptionId", module)
        value = module_parameters(module)["keyVaultResourceId"]
        node = expression(value)
        self.assertEqual(node.name, "if")
        self.assertEqual(node.args[1].name, "createObject")
        self.assertEqual(node.args[1].args[0], "value")
        self.assert_module_output(node.args[1].args[1], self.vault_module(), "keyVaultResourceId")
        self.assertEqual(ArmSubset(self.main, {"enableKeyVault": False})(value), {"value": ""})
        template = self.templates["foundry-private-endpoints"]
        endpoint = next(
            resource for resource in resources(template, "Microsoft.Network/privateEndpoints")
            if resource["properties"]["privateLinkServiceConnections"][0]["properties"]["groupIds"] == ["vault"]
        )
        group = next(
            resource for resource in resources(template, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups")
            if resource["properties"]["privateDnsZoneConfigs"][0]["name"] == "key-vault"
        )
        vault_id = f"/subscriptions/{OTHER_SUBSCRIPTION}/resourceGroups/external/providers/Microsoft.KeyVault/vaults/ca"
        for target in ("", vault_id):
            evaluator = ArmSubset(template, {"keyVaultResourceId": target})
            for resource in (endpoint, group):
                self.assertEqual(evaluator(resource["condition"]), bool(target))
            connection = endpoint["properties"]["privateLinkServiceConnections"][0]
            self.assertEqual(evaluator(connection["properties"]["privateLinkServiceId"]), target)
        self.assert_expression(
            group["properties"]["privateDnsZoneConfigs"][0]["properties"]["privateDnsZoneId"],
            "[parameters('dnsZoneIds').keyVault]",
        )
        for output in ("keyVaultName", "keyVaultResourceId"):
            value = self.main["outputs"][output]["value"]
            self.assertEqual(ArmSubset(self.main, {"enableKeyVault": False})(value), "")
            self.assert_module_output(expression(value).args[1], self.vault_module(), output)

    def test_telemetry_roles_are_exactly_two_component_scoped_project_readers(self):
        template = self.templates["foundry-roles"]
        readers = next(resource for resource in template["resources"] if "copy" in resource)
        self.assertEqual(set(template["variables"]["telemetryReaderRoles"]), TELEMETRY_ROLES)
        self.assert_expression(
            readers["scope"], "[resourceId('Microsoft.Insights/components', parameters('appInsightsName'))]"
        )
        self.assert_expression(readers["properties"]["principalId"], "[parameters('projectPrincipalId')]")
        self.assertEqual(readers["properties"]["principalType"], "ServicePrincipal")
        self.assert_expression(
            readers["properties"]["roleDefinitionId"],
            "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
            "variables('telemetryReaderRoles')[copyIndex()])]",
        )
        self.assert_expression(
            readers["name"], "[guid(parameters('projectPrincipalId'), "
            "variables('telemetryReaderRoles')[copyIndex()], "
            "resourceId('Microsoft.Insights/components', parameters('appInsightsName')))]",
        )
        telemetry = phase_module(self.main, "telemetry")
        self.assert_module_output(
            module_parameters(telemetry)["projectPrincipalId"]["value"],
            self.module("project"), "projectPrincipalId",
        )
        self.assert_module_output(
            module_parameters(telemetry)["appInsightsName"]["value"],
            self.module("diagnostics"), "appInsightsName",
        )

    def test_role_phases_do_not_leak_data_roles_into_telemetry(self):
        template = self.templates["foundry-roles"]
        for phase, expected in (("pre", 4), ("post", 2), ("all", 6), ("telemetry", 2)):
            evaluator = ArmSubset(template, {"phase": phase})
            count = sum(
                evaluator(resource["copy"]["count"]) if "copy" in resource else 1
                for resource in template["resources"] if evaluator(resource["condition"])
            )
            self.assertEqual(count, expected, phase)
        self.assertEqual(
            set(template["parameters"]["phase"]["allowedValues"]), {"pre", "post", "all", "telemetry"}
        )

    def test_storage_owner_guid_preserves_existing_assignment_identity(self):
        template = self.templates["foundry-roles"]
        owner = next(
            resource for resource in resources(template, "Microsoft.Authorization/roleAssignments")
            if "conditionVersion" in resource["properties"]
        )
        self.assert_expression(
            owner["name"],
            f"[guid(subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '{STORAGE_OWNER}'), "
            "resourceId('Microsoft.Storage/storageAccounts', parameters('storageName')), "
            "parameters('projectPrincipalId'), parameters('uniqueSuffix'))]",
        )
        self.assert_expression(
            owner["scope"], "[resourceId('Microsoft.Storage/storageAccounts', parameters('storageName'))]"
        )
        self.assertEqual(owner["properties"]["conditionVersion"], "2.0")
        for workspace in ("11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
            condition = ArmSubset(template, {"projectWorkspaceIdGuid": workspace})(
                owner["properties"]["condition"]
            )
            self.assertEqual(condition, (
                "((!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read'})"
                "  AND  !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/filter/action'})"
                " AND  !(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/write'}) )"
                " OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name]"
                f" StringStartsWithIgnoreCase '{workspace}' AND"
                " @Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name]"
                " StringLikeIgnoreCase '*-azureml-agent'))"
            ))

    def test_cosmos_data_contributor_role_0002_and_project_identity_are_preserved(self):
        template = self.templates["foundry-roles"]
        assignment = self.one(template, "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments")
        self.assert_expression(assignment["properties"]["principalId"], "[parameters('projectPrincipalId')]")
        self.assert_expression(
            assignment["properties"]["roleDefinitionId"], "[variables('cosmosDataPlaneRoleDefinitionId')]"
        )
        self.assert_expression(
            template["variables"]["cosmosDataPlaneRoleDefinitionId"],
            "[resourceId('Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions', "
            "parameters('cosmosDBName'), '00000000-0000-0000-0000-000000000002')]",
        )
        scope = ArmSubset(template, {"cosmosDBName": "cosmos-contract"})(
            assignment["properties"]["scope"]
        )
        self.assertEqual(scope, (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/stack-rg"
            "/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-contract"
        ))

    def test_add_project_optional_telemetry_normalizes_cross_scope_id(self):
        template = self.templates["add-project"]
        self.assertEqual(template["parameters"]["existingAppInsightsResourceId"]["defaultValue"], "")
        telemetry = phase_module(template, "telemetry")
        resource_id = (
            f"/subscriptions/{OTHER_SUBSCRIPTION}/resourceGroups/monitoring-rg"
            "/providers/Microsoft.Insights/components/shared-appi"
        )
        for supplied in ("", " \t", f" \n{resource_id}\t "):
            evaluator = ArmSubset(template, {"existingAppInsightsResourceId": supplied})
            enabled = bool(supplied.strip())
            self.assertEqual(evaluator(telemetry["condition"]), enabled)
            self.assertEqual(
                evaluator(telemetry["subscriptionId"]), OTHER_SUBSCRIPTION if enabled else SUBSCRIPTION
            )
            self.assertEqual(
                evaluator(telemetry["resourceGroup"]), "monitoring-rg" if enabled else "stack-rg"
            )
            if enabled:
                self.assertEqual(
                    evaluator(module_parameters(telemetry)["appInsightsName"]["value"]), "shared-appi"
                )

    def test_add_project_reuses_connection_and_grants_only_its_own_principal(self):
        template = self.templates["add-project"]
        identity = next(
            resource for resource in template["resources"]
            if "uniqueConnectionSuffix" in module_parameters(resource)
        )
        for phase in ("pre", "post", "telemetry"):
            self.assert_module_output(
                module_parameters(phase_module(template, phase))["projectPrincipalId"]["value"],
                identity, "projectPrincipalId",
            )
        forbidden = {
            "microsoft.insights/components", "microsoft.operationalinsights/workspaces",
            "microsoft.insights/privatelinkscopes", "microsoft.cognitiveservices/accounts/connections",
        }
        self.assertFalse(forbidden & {resource["type"].lower() for resource in walk_resources(template)})
        for phase in ("pre", "post"):
            self.assert_expression(
                module_parameters(phase_module(template, phase))["uniqueSuffix"]["value"],
                "[variables('uniqueSuffix')]",
            )

    def test_foundry_is_entra_only_with_system_assigned_account_and_project(self):
        template = self.templates["foundry"]
        account = self.one(template, "Microsoft.CognitiveServices/accounts")
        self.assertEqual(account["identity"], {"type": "SystemAssigned"})
        self.assertIs(account["properties"]["disableLocalAuth"], True)
        self.assertEqual(account["properties"]["publicNetworkAccess"], "Disabled")
        self.assertEqual(account["properties"]["networkAcls"]["defaultAction"], "Deny")
        project = self.one(self.templates["foundry-identity"], "Microsoft.CognitiveServices/accounts/projects")
        self.assertEqual(project["identity"], {"type": "SystemAssigned"})
        for name, output, resource_type in (
            ("foundry", "accountPrincipalId", "Microsoft.CognitiveServices/accounts"),
            ("foundry-identity", "projectPrincipalId", "Microsoft.CognitiveServices/accounts/projects"),
        ):
            principal = expression(self.templates[name]["outputs"][output]["value"])
            self.assertEqual(principal.key, "principalId")
            self.assertEqual(principal.value.key, "identity")
            self.assertEqual(principal.value.value.args[-1], "full")
            self.assertEqual(principal.value.value.args[0].args[0], resource_type)

    def test_acr_retains_project_acrpull_and_optional_allowlist(self):
        template = self.templates["container-registry"]
        acr = self.one(template, "Microsoft.ContainerRegistry/registries")
        self.assertEqual(acr["sku"]["name"], "Premium")
        self.assertIs(acr["properties"]["adminUserEnabled"], False)
        for cidr in ("", "203.0.113.0/26"):
            evaluator = ArmSubset(template, {"developerIpCidr": cidr})
            self.assertEqual(
                evaluator(acr["properties"]["publicNetworkAccess"]), "Enabled" if cidr else "Disabled"
            )
            rules = evaluator(acr["properties"]["networkRuleSet"])
            self.assertEqual(rules, {
                "defaultAction": "Deny", "ipRules": [{"action": "Allow", "value": cidr}]
            } if cidr else None)
        role = self.one(template, "Microsoft.Authorization/roleAssignments")
        self.assertEqual(template["variables"]["acrPullRoleId"], "7f951dda-4ed3-4680-a7ca-43fe172d538d")
        self.assert_expression(role["properties"]["principalId"], "[parameters('projectPrincipalId')]")
        self.assert_module_output(
            module_parameters(self.module("acr"))["projectPrincipalId"]["value"],
            self.module("project"), "projectPrincipalId",
        )

    def test_vm_password_is_secure_and_patching_is_automatic_by_platform(self):
        for template in (self.main, self.templates["vm"]):
            parameter = template["parameters"]["adminPassword"]
            self.assertEqual(parameter["type"].lower(), "securestring")
            self.assertNotIn("defaultValue", parameter)
            self.assertNotIn("adminPassword", template.get("outputs", {}))
        vm = self.one(self.templates["vm"], "Microsoft.Compute/virtualMachines")
        profile = vm["properties"]["osProfile"]
        self.assert_expression(profile["adminPassword"], "[parameters('adminPassword')]")
        self.assert_expression(
            module_parameters(self.module("vm"))["adminPassword"]["value"], "[parameters('adminPassword')]"
        )
        self.assertEqual(profile["windowsConfiguration"]["patchSettings"]["patchMode"], "AutomaticByPlatform")
        self.assertIs(profile["windowsConfiguration"]["enableAutomaticUpdates"], True)
        self.assertIs(profile["windowsConfiguration"]["provisionVMAgent"], True)

    def test_diagnostics_fanout_uses_outer_full_resource_ids_and_skiplogs(self):
        template = self.templates["diagnostics"]
        fanout = self.one(template, "Microsoft.Resources/deployments")
        self.assertEqual(fanout["properties"]["expressionEvaluationOptions"]["scope"], "outer")
        self.assertEqual(fanout["properties"]["mode"], "Incremental")
        diagnostic = self.one(
            fanout["properties"]["template"], "Microsoft.Insights/diagnosticSettings"
        )
        full_id = (
            f"/subscriptions/{OTHER_SUBSCRIPTION}/resourceGroups/other-rg"
            "/providers/Microsoft.Storage/storageAccounts/target"
        )
        for skip in (None, False, True):
            target = {"name": "target", "resourceId": full_id}
            if skip is not None:
                target["skipLogs"] = skip
            evaluator = ArmSubset(template, {"targets": [target]})
            self.assertEqual(evaluator(fanout["copy"]["count"]), 1)
            self.assertEqual(evaluator(diagnostic["scope"]), full_id)
            self.assertEqual(evaluator(diagnostic["properties"]["logs"]), [] if skip else [
                {"categoryGroup": "allLogs", "enabled": True}
            ])
        self.assertEqual(diagnostic["properties"]["metrics"], [{"category": "AllMetrics", "enabled": True}])
        self.assertEqual(diagnostic["properties"]["workspaceId"], template["outputs"]["lawId"]["value"])
        self.assertEqual(diagnostic["properties"]["storageAccountId"], template["outputs"]["diagStorageId"]["value"])

    def test_main_diagnostic_targets_use_resource_outputs_and_storage_skips_logs(self):
        targets = expression(module_parameters(self.module("diagnostics"))["targets"]["value"])
        objects = {
            obj.args[1]: dict(zip(obj.args[::2], obj.args[1::2]))
            for obj in calls(targets, "createObject") if obj.args[0] == "name"
        }
        expected = {
            "hubVnet": ("networking", "hubVnetId"), "vmVnet": ("networking", "vmVnetId"),
            "aiappVnet": ("networking", "aiappVnetId"), "aiSearch": ("foundryDeps", "aiSearchID"),
            "cosmosDB": ("foundryDeps", "cosmosDBId"), "storage": ("foundryDeps", "azureStorageId"),
            "aiAccount": ("foundry", "accountID"), "firewall": ("firewall", "firewallId"),
            "firewallPip": ("firewall", "dataPipId"), "bastion": ("bastion", "bastionId"),
            "bastionPip": ("bastion", "bastionPipId"),
        }
        self.assertEqual(set(objects), set(expected))
        for target, (module, output) in expected.items():
            self.assert_module_output(objects[target]["resourceId"], self.module(module), output)
        self.assertEqual(objects["storage"]["skipLogs"], Call("true", ()))

    def test_securitycontrol_tag_cannot_be_overridden_for_new_resources(self):
        for name in ("main", "diagnostics", "key-vault", "dns", "foundry-private-endpoints"):
            evaluator = ArmSubset(self.templates[name], {"tags": {"SecurityControl": "Other", "owner": "test"}})
            self.assertEqual(evaluator.variable("commonTags"), {"SecurityControl": "Ignore", "owner": "test"})

    def test_main_parameter_example_compiles_without_secret_resolution(self):
        parameters, template = self.compile_example("main")
        self.assertEqual(parameters["adminPassword"], {"value": NONSECRET})
        self.assertEqual(template["parameters"]["adminPassword"]["type"].lower(), "securestring")
        self.assertEqual(parameters["modelName"]["value"], "gpt-4o-mini")
        self.assertEqual(parameters["enableContainerRegistry"]["value"], True)

    def test_add_project_parameter_example_compiles_against_real_template(self):
        parameters, template = self.compile_example("add-project")
        self.assertEqual(parameters["projectName"]["value"], "project2")
        self.assertIn("existingAppInsightsResourceId", template["parameters"])
        self.assertEqual(template["parameters"]["existingAppInsightsResourceId"]["defaultValue"], "")

    def compile_example(self, stem):
        source = ROOT / f"{stem}.bicepparam.example"
        with tempfile.TemporaryDirectory(prefix=".infrastructure-contracts-", dir=ROOT / "tests") as directory:
            path = Path(directory) / f"{stem}.bicepparam"
            path.write_text(
                sanitized_example(source.read_text(encoding="utf-8"), ROOT / f"{stem}.bicep", path),
                encoding="utf-8",
            )
            compiled = compile_file(path, self.environment, "build-params")
        self.assertFalse(Path(directory).exists())
        self.assertIsNone(compiled.get("templateSpecId"))
        parameters = json.loads(compiled["parametersJson"])["parameters"]
        for name, value in parameters.items():
            self.assertEqual(set(value), {"value"}, f"Unresolved secret reference in {name}")
        return parameters, json.loads(compiled["templateJson"])


class RunnerSafetyContracts(unittest.TestCase):
    def test_upgrade_notice_is_allowed(self):
        result = subprocess.CompletedProcess([], 0, "{}", (
            'WARNING: A new Bicep release is available: v0.47.16. '
            'Upgrade now by running "az bicep upgrade".\n'
        ))
        self.assertEqual(check_compiler_result(result, "test"), {})

    def test_bicep_warnings_and_errors_are_not_hidden(self):
        for code, diagnostic in (
            (0, "Warning BCP318: nullable resource reference"),
            (0, "Warning no-unused-params: unused parameter"),
            (1, "Error BCP104: referenced module has errors"),
        ):
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaises(AssertionError):
                    check_compiler_result(
                        subprocess.CompletedProcess([], code, "{}", diagnostic), "test"
                    )

    def test_evaluator_rejects_runtime_functions_instead_of_inventing_results(self):
        evaluator = ArmSubset({})
        with self.assertRaisesRegex(ValueError, "outside"):
            evaluator("[reference('not-a-real-resource')]")
        self.assertEqual(evaluator("[if(false(), reference('not-evaluated'), '')]"), "")

    def test_parameter_sanitizer_replaces_secrets_and_rejects_remaining_azure_calls(self):
        destination = ROOT / "tests" / "scratch" / "example.bicepparam"
        sanitized = sanitized_example(
            "using 'main.bicep'\nparam password = az.getSecret('sub', 'rg', 'vault', 'secret')\n",
            ROOT / "main.bicep", destination,
        )
        self.assertNotIn("az.getSecret", sanitized)
        self.assertIn(f"param password = '{NONSECRET}'", sanitized)
        self.assertIn("using '../../main.bicep'", sanitized)
        with self.assertRaisesRegex(AssertionError, "unsanitized"):
            sanitized_example(
                "using 'main.bicep'\nparam x = az.otherFunction()\n",
                ROOT / "main.bicep", destination,
            )


if __name__ == "__main__":
    unittest.main()
