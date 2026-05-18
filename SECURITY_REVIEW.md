# Security & Best Practice Review: aifoundrybyov2 Terraform Template

> **Review Date:** January 2026  
> **Template:** Azure AI Foundry with BYO VNet (Hub-Spoke Architecture)  
> **Status:** Comprehensive security and Terraform best practices review

---

## 📋 Table of Contents

- [Executive Summary](#executive-summary)
- [Security Findings](#security-findings)
  - [Critical Issues](#critical-issues)
  - [High Priority Issues](#high-priority-issues)
  - [Medium Priority Issues](#medium-priority-issues)
  - [Low Priority Issues](#low-priority-issues)
- [Terraform Best Practices](#terraform-best-practices)
  - [Code Organization](#code-organization)
  - [Variable Management](#variable-management)
  - [State Management](#state-management)
  - [Resource Naming](#resource-naming)
  - [Module Structure](#module-structure)
- [Recommendations Summary](#recommendations-summary)
- [Compliance Considerations](#compliance-considerations)

---

## Executive Summary

This review covers the `aifoundrybyov2` Terraform template which deploys Azure AI Foundry in a hub-spoke network topology. The template demonstrates good practices in several areas including:

✅ **Strengths:**
- Private endpoints for all PaaS services
- Azure Firewall for centralized egress control
- Comprehensive diagnostic logging
- VNet Flow Logs with Traffic Analytics
- Managed Identity usage with RBAC
- TLS 1.2 enforcement on storage accounts
- Azure AD authentication for Terraform state backend

⚠️ **Areas for Improvement:**
- Overly permissive firewall application rules
- NSG rule allowing RDP from any source
- AI Search local authentication not disabled
- Missing resource locks for critical resources
- Hardcoded values in terraform.tfvars
- Provider version constraints too permissive

---

## Security Findings

### Critical Issues

#### 1. NSG Rule Allows RDP from Any Source Address (firewall.tf:247-260)

**Issue:** The NSG rule `AllowRDP` has `source_address_prefix = "*"` which allows RDP (port 3389) from any IP address on the internet.

**Current Code:**
```hcl
resource "azurerm_network_security_rule" "allow_rdp" {
  count                       = var.vmdeploy ? 1 : 0
  name                        = "AllowRDP"
  priority                    = 1000
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "3389"
  source_address_prefix       = "*"        # ⚠️ CRITICAL - Open to internet
  destination_address_prefix  = "*"
  # ...
}
```

**Risk:** Exposes VM to brute-force attacks, credential stuffing, and potential unauthorized access.

**Recommendation:**
```hcl
resource "azurerm_network_security_rule" "allow_rdp" {
  count                       = var.vmdeploy ? 1 : 0
  name                        = "AllowRDP"
  priority                    = 1000
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "3389"
  # Option 1: Allow only from Azure Bastion subnet
  source_address_prefix       = azurerm_subnet.hub_bastion.address_prefixes[0]
  # Option 2: Allow only from specific corporate IP range (add as variable)
  # source_address_prefix     = var.allowed_rdp_source_cidr
  destination_address_prefix  = "*"
  # ...
}
```

**Alternative:** Remove this rule entirely and use Azure Bastion for secure RDP access. The template already provisions the Bastion subnet.

---

#### 2. Overly Permissive Firewall Application Rules (firewall.tf:38-58)

**Issue:** The firewall application rule allows HTTP/HTTPS traffic from any source (`*`) to any destination FQDN (`*`).

**Current Code:**
```hcl
application_rule_collection {
  name     = "AllowWebTraffic"
  priority = 100
  action   = "Allow"

  rule {
    name = "AllowHttpHttps"
    source_addresses = ["*"]      # ⚠️ Too permissive
    destination_fqdns = ["*"]     # ⚠️ Too permissive

    protocols {
      type = "Http"
      port = 80
    }
    protocols {
      type = "Https"
      port = 443
    }
  }
}
```

**Risk:** Defeats the purpose of having a firewall; allows data exfiltration to any internet destination.

**Recommendation:**
```hcl
application_rule_collection {
  name     = "AllowAzureAIServices"
  priority = 100
  action   = "Allow"

  rule {
    name = "AllowAzureAI"
    source_addresses = [
      var.vnetconfig.vm_vnet_prefix,
      var.vnetconfig.aiapp_vnet_prefix
    ]
    destination_fqdn_tags = [
      "AzureCognitiveSearch",
      "AzureOpenAI"
    ]
    protocols {
      type = "Https"
      port = 443
    }
  }
  
  rule {
    name = "AllowAzureMonitor"
    source_addresses = [
      var.vnetconfig.vm_vnet_prefix,
      var.vnetconfig.aiapp_vnet_prefix
    ]
    destination_fqdn_tags = ["AzureMonitor"]
    protocols {
      type = "Https"
      port = 443
    }
  }
}

# Separate rule for Windows Updates (if VM is deployed)
application_rule_collection {
  name     = "AllowWindowsUpdate"
  priority = 200
  action   = "Allow"

  rule {
    name = "WindowsUpdate"
    source_addresses = [var.vnetconfig.vm_vnet_prefix]
    destination_fqdn_tags = ["WindowsUpdate"]
    protocols {
      type = "Https"
      port = 443
    }
  }
}
```

---

### High Priority Issues

#### 3. AI Search Local Authentication Not Disabled (foundry.tf:72)

**Issue:** AI Search has `disableLocalAuth = false`, meaning API keys can be used for authentication.

**Current Code:**
```hcl
properties = {
  # ...
  disableLocalAuth    = false    # ⚠️ Should be true
  publicNetworkAccess = "Disabled"
  # ...
}
```

**Risk:** API keys can be leaked or compromised; harder to audit and rotate than managed identities.

**Recommendation:**
```hcl
properties = {
  # ...
  disableLocalAuth    = true     # ✅ Force Azure AD only
  publicNetworkAccess = "Disabled"
  # ...
}
```

---

#### 4. AI Foundry Local Authentication Not Disabled (foundry.tf:105)

**Issue:** Similar to AI Search, the AI Foundry resource has `disableLocalAuth = false`.

**Current Code:**
```hcl
properties = {
  disableLocalAuth       = false    # ⚠️ Should be true
  allowProjectManagement = true
  # ...
}
```

**Recommendation:**
```hcl
properties = {
  disableLocalAuth       = true     # ✅ Force Azure AD only
  allowProjectManagement = true
  # ...
}
```

---

#### 5. Subscription ID and Key Vault ID Exposed in terraform.tfvars (terraform.tfvars:2,11)

**Issue:** Sensitive configuration values are hardcoded in the terraform.tfvars file which could be committed to version control.

**Current Code:**
```hcl
subscription_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
# ...
kvid = "/subscriptions/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/resourceGroups/<resource-group>/providers/Microsoft.KeyVault/vaults/<keyvault-name>"
```

**Risk:** Exposes Azure subscription IDs and Key Vault names in source control.

**Recommendation:**
1. Use environment variables:
```bash
export TF_VAR_subscription_id="your-subscription-id"
export TF_VAR_kvid="your-keyvault-id"
```

2. Or use a separate `terraform.tfvars.local` file added to `.gitignore`

3. Create a `.tfvars.example` template:
```hcl
# terraform.tfvars.example
subscription_id = "<YOUR_SUBSCRIPTION_ID>"
kvid = "<YOUR_KEYVAULT_RESOURCE_ID>"
```

---

#### 6. Missing Resource Locks for Critical Resources

**Issue:** No resource locks are defined to prevent accidental deletion of critical infrastructure.

**Recommendation:** Add delete locks for production-critical resources:

```hcl
resource "azurerm_management_lock" "rg_lock" {
  name       = "delete-lock"
  scope      = azurerm_resource_group.rg.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of AI Foundry resources"
}

resource "azurerm_management_lock" "cosmosdb_lock" {
  name       = "delete-lock"
  scope      = azurerm_cosmosdb_account.cosmosdb.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of agent thread data"
}

resource "azurerm_management_lock" "storage_lock" {
  name       = "delete-lock"
  scope      = azurerm_storage_account.storage_account.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of AI data"
}
```

---

### Medium Priority Issues

#### 7. CosmosDB Zone Redundancy Disabled (foundry.tf:47)

**Issue:** CosmosDB geo_location has `zone_redundant = false`, reducing availability.

**Recommendation:**
```hcl
geo_location {
  location          = azurerm_resource_group.rg.location
  failover_priority = 0
  zone_redundant    = true    # ✅ Enable for production
}
```

---

#### 8. Flow Logs Storage Account Missing Network Rules (diagnostics.tf:356-367)

**Issue:** The `flowlogs_storage` account doesn't have the same network security as the primary storage account.

**Current Code:**
```hcl
resource "azurerm_storage_account" "flowlogs_storage" {
  # ...
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  # ⚠️ Missing: shared_access_key_enabled = false
  # ⚠️ Missing: network_rules block
}
```

**Recommendation:**
```hcl
resource "azurerm_storage_account" "flowlogs_storage" {
  # ...
  shared_access_key_enabled       = false
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices", "Logging", "Metrics"]
  }
}
```

---

#### 9. Provider Version Constraints Too Permissive (providers.tf:4-20)

**Issue:** Using `>= X.0` allows any major version upgrade which could introduce breaking changes.

**Current Code:**
```hcl
required_providers {
  azurerm = {
    source  = "hashicorp/azurerm"
    version = ">= 4.0"    # ⚠️ Too permissive
  }
  azapi = {
    source  = "azure/azapi"
    version = ">= 2.0"    # ⚠️ Too permissive
  }
  # ...
}
```

**Recommendation:** Use pessimistic version constraints:
```hcl
required_providers {
  azurerm = {
    source  = "hashicorp/azurerm"
    version = "~> 4.0"    # ✅ Allows 4.x only
  }
  azapi = {
    source  = "azure/azapi"
    version = "~> 2.0"    # ✅ Allows 2.x only
  }
  random = {
    source  = "hashicorp/random"
    version = "~> 3.0"
  }
  time = {
    source  = "hashicorp/time"
    version = "~> 0.9"
  }
}
```

---

#### 10. VM OS Disk Using Standard_LRS (vm.tf:36-38)

**Issue:** The jump box VM uses `Standard_LRS` which provides no redundancy.

**Recommendation:**
```hcl
os_disk {
  storage_account_type = "Premium_LRS"    # ✅ Better performance
  # Or "StandardSSD_ZRS" for zone redundancy
  caching              = "ReadWrite"
}
```

---

#### 11. Missing NSGs for PE and Agents Subnets

**Issue:** The `aiapp_pe` and `aiapp_agents` subnets don't have Network Security Groups attached.

**Recommendation:** Add NSGs with deny-all inbound rules except for necessary traffic:

```hcl
resource "azurerm_network_security_group" "pe_nsg" {
  name                = "${var.prefix}-nsg-pe-${random_string.random.result}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_network_security_rule" "pe_deny_inbound_internet" {
  name                        = "DenyInternetInbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.pe_nsg.name
}

resource "azurerm_subnet_network_security_group_association" "pe_nsg" {
  subnet_id                 = azurerm_subnet.aiapp_pe.id
  network_security_group_id = azurerm_network_security_group.pe_nsg.id
}
```

---

### Low Priority Issues

#### 12. Log Analytics Retention Only 30 Days (firewall.tf:9)

**Issue:** 30-day retention may not meet compliance requirements for audit logs.

**Recommendation:**
```hcl
resource "azurerm_log_analytics_workspace" "law" {
  # ...
  retention_in_days = 90    # ✅ Typical compliance requirement
  # Or use variable for flexibility:
  # retention_in_days = var.log_retention_days
}
```

---

#### 13. Bastion SKU is Basic (firewall.tf:285)

**Issue:** Basic SKU lacks features like native client support and IP-based connection.

**Recommendation:**
```hcl
resource "azurerm_bastion_host" "bastion" {
  # ...
  sku = "Standard"    # ✅ Required for native client & advanced features
  
  # Enable advanced features
  copy_paste_enabled     = true
  file_copy_enabled      = true
  tunneling_enabled      = true
  shareable_link_enabled = false
}
```

---

#### 14. Missing Tags on Resources

**Issue:** Resources lack consistent tagging for cost allocation and governance.

**Recommendation:** Add a `tags` variable and apply to all resources:

```hcl
# In variables.tf
variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {
    Environment = "Production"
    Project     = "AI Foundry"
    ManagedBy   = "Terraform"
  }
}

# In locals.tf
locals {
  common_tags = merge(var.tags, {
    DeploymentId = random_string.random.result
  })
}

# Apply to resources
resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-${random_string.random.result}"
  location = var.location
  tags     = local.common_tags
}
```

---

## Terraform Best Practices

### Code Organization

| Current State | Recommendation |
|--------------|----------------|
| ✅ Separate files for logical groups | Good - maintains readability |
| ⚠️ `locals.tf` contains resources | Move `random_string` and `data` sources to appropriate files |
| ⚠️ Large `foundry.tf` file | Consider splitting into `ai-services.tf` and `data-services.tf` |

### Variable Management

| Current State | Recommendation |
|--------------|----------------|
| ✅ Variables have descriptions | Good practice |
| ✅ Sensitive variables marked | `admin_password` is marked sensitive |
| ⚠️ No validation rules | Add validation for CIDR blocks, prefix length |

**Example Validation:**
```hcl
variable "prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "aifoundryv2"
  
  validation {
    condition     = length(var.prefix) <= 10 && can(regex("^[a-z0-9]+$", var.prefix))
    error_message = "Prefix must be lowercase alphanumeric and max 10 characters."
  }
}

variable "fwsku" {
  description = "Azure Firewall SKU"
  type        = string
  default     = "Standard"
  
  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.fwsku)
    error_message = "Firewall SKU must be Basic, Standard, or Premium."
  }
}
```

### State Management

| Current State | Recommendation |
|--------------|----------------|
| ✅ Remote backend (Azure Blob) | Good practice |
| ✅ Azure AD auth for state | Excellent security |
| ⚠️ Backend values hardcoded | Consider using backend config file |

**Recommendation:**
```hcl
# backend.tf
terraform {
  backend "azurerm" {}
}

# backend.hcl (not committed to repo)
resource_group_name  = "Management"
storage_account_name = "<your-storage-account>"
container_name       = "tfstate"
key                  = "aifoundrybyov2.tfstate"
use_azuread_auth     = true
use_oidc             = true
```

Then initialize with:
```bash
terraform init -backend-config=backend.hcl
```

### Resource Naming

| Current State | Recommendation |
|--------------|----------------|
| ✅ Consistent naming pattern | Good - uses prefix and random suffix |
| ⚠️ Some resources use different patterns | Standardize all names |
| ⚠️ Storage accounts limited by naming rules | Consider using separate naming for storage |

**Recommendation:** Create a naming module or use Azure CAF naming convention:
```hcl
locals {
  naming = {
    resource_group  = "${var.prefix}-rg-${random_string.random.result}"
    vnet            = "${var.prefix}-vnet-{purpose}-${random_string.random.result}"
    storage_account = "st${var.prefix}${random_string.random.result}"  # 3-24 lowercase
    cosmos_account  = "${var.prefix}-cosmos-${random_string.random.result}"
  }
}
```

### Module Structure

**Recommendation:** Consider refactoring into modules for reusability:

```
aifoundrybyov2/
├── main.tf              # Root module orchestration
├── variables.tf         # Root variables
├── outputs.tf           # Root outputs
├── terraform.tfvars     # Variable values
└── modules/
    ├── networking/
    │   ├── main.tf
    │   ├── variables.tf
    │   └── outputs.tf
    ├── security/
    │   ├── main.tf      # Firewall, NSGs, Route Tables
    │   ├── variables.tf
    │   └── outputs.tf
    ├── ai-foundry/
    │   ├── main.tf      # AI Foundry, Project, Connections
    │   ├── variables.tf
    │   └── outputs.tf
    └── monitoring/
        ├── main.tf      # Diagnostics, Flow Logs
        ├── variables.tf
        └── outputs.tf
```

---

## Recommendations Summary

### Priority Matrix

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| 🔴 Critical | NSG RDP from * | Low | High |
| 🔴 Critical | Firewall allow all | Medium | High |
| 🟠 High | AI Search local auth | Low | Medium |
| 🟠 High | AI Foundry local auth | Low | Medium |
| 🟠 High | Sensitive values in tfvars | Low | Medium |
| 🟠 High | Missing resource locks | Low | Medium |
| 🟡 Medium | CosmosDB zone redundancy | Low | Medium |
| 🟡 Medium | Flow logs storage security | Low | Low |
| 🟡 Medium | Provider version constraints | Low | Low |
| 🟡 Medium | Missing PE/Agents NSGs | Medium | Medium |
| 🟢 Low | Log retention 30 days | Low | Low |
| 🟢 Low | Missing tags | Low | Low |
| 🟢 Low | Bastion Basic SKU | Low | Low |

### Quick Wins (Immediate)

1. **Restrict NSG RDP source** to Bastion subnet or specific IPs
2. **Disable local authentication** on AI Search and AI Foundry
3. **Add version constraints** (~> instead of >=) to providers
4. **Add resource locks** to prevent accidental deletion

### Short-Term (1-2 Sprints)

1. **Refactor firewall rules** to use FQDN tags and specific sources
2. **Add NSGs** to PE and Agents subnets
3. **Implement tagging strategy** across all resources
4. **Move sensitive values** to environment variables or Key Vault

### Long-Term (Roadmap)

1. **Modularize the template** for reusability
2. **Implement Azure Policy** for governance
3. **Add Terraform testing** with Terratest or terraform-compliance
4. **Enable Customer-Managed Keys** for data encryption

---

## Compliance Considerations

### SOC 2

| Control | Status | Notes |
|---------|--------|-------|
| Access Control | ⚠️ Partial | RDP rule needs restriction |
| Encryption | ✅ Met | TLS 1.2, encryption at rest |
| Logging | ✅ Met | Comprehensive diagnostics |
| Network Security | ⚠️ Partial | Firewall rules too permissive |

### ISO 27001

| Control | Status | Notes |
|---------|--------|-------|
| A.13.1 Network Security | ⚠️ Partial | NSG improvements needed |
| A.9.4 Access Control | ⚠️ Partial | Disable local auth |
| A.12.4 Logging | ✅ Met | All resources logged |

### Azure Well-Architected Framework

| Pillar | Score | Notes |
|--------|-------|-------|
| Security | 7/10 | Fix RDP, firewall rules, local auth |
| Reliability | 8/10 | Enable zone redundancy |
| Cost Optimization | 8/10 | Consider right-sizing |
| Operational Excellence | 7/10 | Add tagging, modules |
| Performance | 9/10 | Good architecture |

---

## Conclusion

The `aifoundrybyov2` template provides a solid foundation for deploying Azure AI Foundry in an enterprise environment. The hub-spoke architecture, private endpoints, and comprehensive logging demonstrate security-conscious design.

**Key actions to improve security posture:**

1. 🔴 **Immediately** restrict NSG RDP rule and tighten firewall rules
2. 🟠 **Soon** disable local authentication on AI services
3. 🟡 **Plan** for resource locks, tagging, and modularization

Implementing these recommendations will significantly improve the security posture and maintainability of this infrastructure-as-code deployment.

---

*This review was conducted against Terraform best practices, Azure Well-Architected Framework, and industry security standards.*

---

## 🛠️ Implementation Plan

This section provides the exact code changes required to implement all recommendations from this review.

### Phase 1: Critical Fixes

#### 1.1 Restrict NSG RDP Rule (firewall.tf)

**Find and replace** the NSG rule to only allow RDP from Bastion subnet:

```hcl
# BEFORE (REMOVE)
resource "azurerm_network_security_rule" "allow_rdp" {
  count                       = var.vmdeploy ? 1 : 0
  name                        = "AllowRDP"
  priority                    = 1000
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "3389"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.vm_nsg[0].name
}

# AFTER (REPLACE WITH)
resource "azurerm_network_security_rule" "allow_rdp" {
  count                       = var.vmdeploy ? 1 : 0
  name                        = "AllowRDPFromBastion"
  priority                    = 1000
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "3389"
  source_address_prefix       = azurerm_subnet.hub_bastion.address_prefixes[0]
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.vm_nsg[0].name
}
```

#### 1.2 Restrict Firewall Application Rules (firewall.tf)

**Replace** the overly permissive application rule collection with specific FQDN tags:

```hcl
# BEFORE (REMOVE)
application_rule_collection {
  name     = "AllowWebTraffic"
  priority = 100
  action   = "Allow"

  rule {
    name = "AllowHttpHttps"
    source_addresses = ["*"]
    destination_fqdns = ["*"]

    protocols {
      type = "Http"
      port = 80
    }
    protocols {
      type = "Https"
      port = 443
    }
  }
}

# AFTER (REPLACE WITH)
application_rule_collection {
  name     = "AllowAzureServices"
  priority = 100
  action   = "Allow"

  rule {
    name = "AllowAzureMonitor"
    source_addresses = [
      var.vnetconfig.hub_vnet_prefix,
      var.vnetconfig.vm_vnet_prefix,
      var.vnetconfig.aiapp_vnet_prefix
    ]
    destination_fqdn_tags = ["AzureMonitor"]

    protocols {
      type = "Https"
      port = 443
    }
  }

  rule {
    name = "AllowAzureActiveDirectory"
    source_addresses = [
      var.vnetconfig.hub_vnet_prefix,
      var.vnetconfig.vm_vnet_prefix,
      var.vnetconfig.aiapp_vnet_prefix
    ]
    destination_fqdn_tags = ["AzureActiveDirectory"]

    protocols {
      type = "Https"
      port = 443
    }
  }

  rule {
    name = "AllowAzureBackup"
    source_addresses = [
      var.vnetconfig.hub_vnet_prefix,
      var.vnetconfig.vm_vnet_prefix,
      var.vnetconfig.aiapp_vnet_prefix
    ]
    destination_fqdn_tags = ["AzureBackup"]

    protocols {
      type = "Https"
      port = 443
    }
  }
}

application_rule_collection {
  name     = "AllowWindowsUpdate"
  priority = 200
  action   = "Allow"

  rule {
    name = "WindowsUpdate"
    source_addresses = [var.vnetconfig.vm_vnet_prefix]
    destination_fqdn_tags = ["WindowsUpdate"]

    protocols {
      type = "Http"
      port = 80
    }
    protocols {
      type = "Https"
      port = 443
    }
  }
}

application_rule_collection {
  name     = "AllowAIFoundryDependencies"
  priority = 300
  action   = "Allow"

  rule {
    name = "AllowCognitiveServices"
    source_addresses = [var.vnetconfig.aiapp_vnet_prefix]
    destination_fqdns = [
      "*.cognitiveservices.azure.com",
      "*.openai.azure.com",
      "*.services.ai.azure.com"
    ]

    protocols {
      type = "Https"
      port = 443
    }
  }

  rule {
    name = "AllowPythonPackages"
    source_addresses = [var.vnetconfig.aiapp_vnet_prefix]
    destination_fqdns = [
      "pypi.org",
      "*.pypi.org",
      "files.pythonhosted.org",
      "*.anaconda.org",
      "*.anaconda.com"
    ]

    protocols {
      type = "Https"
      port = 443
    }
  }

  rule {
    name = "AllowGitHub"
    source_addresses = [var.vnetconfig.aiapp_vnet_prefix]
    destination_fqdns = [
      "github.com",
      "*.github.com",
      "raw.githubusercontent.com"
    ]

    protocols {
      type = "Https"
      port = 443
    }
  }

  rule {
    name = "AllowHuggingFace"
    source_addresses = [var.vnetconfig.aiapp_vnet_prefix]
    destination_fqdns = [
      "huggingface.co",
      "*.huggingface.co"
    ]

    protocols {
      type = "Https"
      port = 443
    }
  }
}
```

---

### Phase 2: High Priority Fixes

#### 2.1 Disable Local Auth on AI Search (foundry.tf)

```hcl
# In azapi_resource.ai_search, change:
disableLocalAuth    = false
# To:
disableLocalAuth    = true
```

#### 2.2 Disable Local Auth on AI Foundry (foundry.tf)

```hcl
# In azapi_resource.ai_foundry, change:
disableLocalAuth       = false
# To:
disableLocalAuth       = true
```

#### 2.3 Add Resource Locks (new file: locks.tf)

Create a new file `locks.tf`:

```hcl
########## Resource Locks
########## Prevent accidental deletion of critical resources
##########

resource "azurerm_management_lock" "rg_lock" {
  name       = "delete-lock"
  scope      = azurerm_resource_group.rg.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of AI Foundry resource group"
}

resource "azurerm_management_lock" "cosmosdb_lock" {
  name       = "delete-lock"
  scope      = azurerm_cosmosdb_account.cosmosdb.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of agent thread data"
}

resource "azurerm_management_lock" "storage_lock" {
  name       = "delete-lock"
  scope      = azurerm_storage_account.storage_account.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of AI data storage"
}

resource "azurerm_management_lock" "ai_foundry_lock" {
  name       = "delete-lock"
  scope      = azapi_resource.ai_foundry.id
  lock_level = "CanNotDelete"
  notes      = "Prevent accidental deletion of AI Foundry resource"
}
```

#### 2.4 Secure terraform.tfvars

1. Rename current file:
```bash
mv terraform.tfvars terraform.tfvars.local
```

2. Create `terraform.tfvars.example`:
```hcl
# terraform.tfvars.example
# Copy this file to terraform.tfvars.local and fill in your values
# Do NOT commit terraform.tfvars.local to version control

subscription_id = "<YOUR_SUBSCRIPTION_ID>"
location        = "australiaeast"
prefix          = "aifoundryv2"
kvid            = "<YOUR_KEYVAULT_RESOURCE_ID>"
fwprovision     = true
vmdeploy        = true
bastionprovision = false
fwsku           = "Standard"
```

3. Update `.gitignore`:
```
terraform.tfvars
terraform.tfvars.local
*.auto.tfvars
```

4. Update `providers.tf` backend or use environment variables:
```bash
export TF_VAR_subscription_id="your-subscription-id"
export TF_VAR_kvid="your-keyvault-id"
```

---

### Phase 3: Medium Priority Fixes

#### 3.1 Enable CosmosDB Zone Redundancy (foundry.tf)

```hcl
# In azurerm_cosmosdb_account.cosmosdb, change:
geo_location {
  location          = azurerm_resource_group.rg.location
  failover_priority = 0
  zone_redundant    = false
}
# To:
geo_location {
  location          = azurerm_resource_group.rg.location
  failover_priority = 0
  zone_redundant    = true
}
```

#### 3.2 Secure Flow Logs Storage Account (diagnostics.tf)

```hcl
# Update azurerm_storage_account.flowlogs_storage:
resource "azurerm_storage_account" "flowlogs_storage" {
  name                = "flowlogs${random_string.random.result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location

  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"

  shared_access_key_enabled       = false          # ADD THIS
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  network_rules {                                   # ADD THIS BLOCK
    default_action = "Deny"
    bypass         = ["AzureServices", "Logging", "Metrics"]
  }
}
```

#### 3.3 Fix Provider Version Constraints (providers.tf)

```hcl
# Change all >= to ~>
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"    # Changed from >= 4.0
    }
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.0"    # Changed from >= 2.0
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"    # Changed from >= 3.0
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"    # Changed from >= 0.9
    }
  }
  required_version = ">= 1.0"
}
```

#### 3.4 Add NSGs for PE and Agents Subnets (networking.tf)

Add at the end of `networking.tf`:

```hcl
########## NSGs for AI App Subnets
##########

resource "azurerm_network_security_group" "pe_nsg" {
  name                = "${var.prefix}-nsg-pe-${random_string.random.result}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_network_security_rule" "pe_deny_internet_inbound" {
  name                        = "DenyInternetInbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.pe_nsg.name
}

resource "azurerm_subnet_network_security_group_association" "pe_nsg" {
  subnet_id                 = azurerm_subnet.aiapp_pe.id
  network_security_group_id = azurerm_network_security_group.pe_nsg.id
}

resource "azurerm_network_security_group" "agents_nsg" {
  name                = "${var.prefix}-nsg-agents-${random_string.random.result}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_network_security_rule" "agents_deny_internet_inbound" {
  name                        = "DenyInternetInbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.agents_nsg.name
}

resource "azurerm_network_security_rule" "agents_allow_vnet_inbound" {
  name                        = "AllowVNetInbound"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "VirtualNetwork"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.agents_nsg.name
}

resource "azurerm_subnet_network_security_group_association" "agents_nsg" {
  subnet_id                 = azurerm_subnet.aiapp_agents.id
  network_security_group_id = azurerm_network_security_group.agents_nsg.id
}
```

---

### Phase 4: Low Priority Fixes

#### 4.1 Increase Log Analytics Retention (firewall.tf)

```hcl
# In azurerm_log_analytics_workspace.law, change:
retention_in_days = 30
# To:
retention_in_days = 90
```

#### 4.2 Add Tagging Strategy (variables.tf + all resources)

Add to `variables.tf`:

```hcl
variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {
    Environment = "Production"
    Project     = "AI-Foundry"
    ManagedBy   = "Terraform"
    CostCenter  = "AI-Platform"
  }
}
```

Then add `tags = var.tags` to all resources that support it:
- `azurerm_resource_group.rg`
- `azurerm_virtual_network.*`
- `azurerm_storage_account.*`
- `azurerm_cosmosdb_account.cosmosdb`
- `azurerm_log_analytics_workspace.law`
- `azurerm_public_ip.*`
- `azurerm_firewall.fw`
- `azurerm_network_security_group.*`

#### 4.3 Upgrade Bastion to Standard SKU (firewall.tf)

```hcl
# In azurerm_bastion_host.bastion, change:
resource "azurerm_bastion_host" "bastion" {
  count               = var.bastionprovision ? 1 : 0
  name                = "${var.prefix}-bastion-${random_string.random.result}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "Standard"          # Changed from Basic

  copy_paste_enabled     = true             # ADD
  file_copy_enabled      = true             # ADD
  tunneling_enabled      = true             # ADD
  shareable_link_enabled = false            # ADD

  ip_configuration {
    name                 = "configuration"
    subnet_id            = azurerm_subnet.hub_bastion.id
    public_ip_address_id = azurerm_public_ip.bastion_pip[0].id
  }
}
```

---

### Validation Checklist

After implementing changes, run:

```bash
# Format check
terraform fmt -check -recursive

# Validate configuration
terraform validate

# Plan to preview changes
terraform plan -out=security-fixes.tfplan

# Review the plan carefully before applying
terraform show security-fixes.tfplan

# Apply when ready
terraform apply security-fixes.tfplan
```

---

### Expected Resource Changes Summary

| Phase | New Resources | Modified Resources | Deleted Resources |
|-------|---------------|-------------------|-------------------|
| Phase 1 | 0 | 2 (NSG rule, FW policy) | 0 |
| Phase 2 | 4 (resource locks) | 2 (AI Search, AI Foundry) | 0 |
| Phase 3 | 2 (NSGs) + 4 (rules/associations) | 3 (CosmosDB, storage, providers) | 0 |
| Phase 4 | 0 | 3+ (LAW, Bastion, tags) | 0 |
| **Total** | **~10** | **~10** | **0** |

---

### Post-Implementation Security Score

After implementing all phases:

| Pillar | Before | After |
|--------|--------|-------|
| Security | 7/10 | 9/10 |
| Reliability | 8/10 | 9/10 |
| Cost Optimization | 8/10 | 8/10 |
| Operational Excellence | 7/10 | 9/10 |
| Performance | 9/10 | 9/10 |
