# Requires PowerShell 7.3+ and a filesystem enforcing ACLs or Unix permissions.
function Test-RecoveryWindows {
    return [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

function Get-RecoveryWindowsSid {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { return $identity.User }
    finally { $identity.Dispose() }
}

function New-RecoveryWindowsSecurity {
    param([switch]$Directory)

    $sid = Get-RecoveryWindowsSid
    $security = if ($Directory) {
        [Security.AccessControl.DirectorySecurity]::new()
    }
    else {
        [Security.AccessControl.FileSecurity]::new()
    }
    $security.SetOwner($sid)
    $security.SetAccessRuleProtection($true, $false)
    $inheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else {
        [Security.AccessControl.InheritanceFlags]::None
    }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid, [Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance, [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $security.AddAccessRule($rule)
    return $security
}

function Assert-PrivateWindowsSecurity {
    param([Parameter(Mandatory = $true)][object]$Security)

    $sid = Get-RecoveryWindowsSid
    $owner = $Security.GetOwner([Security.Principal.SecurityIdentifier])
    $rules = @($Security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (
        -not $Security.AreAccessRulesProtected -or
        $owner.Value -ne $sid.Value -or
        $rules.Count -ne 1
    ) {
        throw "Recovery ACL must have protected inheritance and only the current user as owner and grantee."
    }
    $rule = $rules[0]
    if (
        $rule.IdentityReference.Value -ne $sid.Value -or
        $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $rule.IsInherited -or
        ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0
    ) {
        throw "Recovery ACL does not grant only the current user full control."
    }
}

function Initialize-WindowsRecoveryDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $directory = [IO.DirectoryInfo]::new($Path)
    if (-not $directory.Exists) {
        $security = New-RecoveryWindowsSecurity -Directory
        [IO.FileSystemAclExtensions]::Create($directory, $security)
        $directory.Refresh()
    }
    if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Recovery directory must not be a symbolic link or junction: $Path"
    }
    $security = [IO.FileSystemAclExtensions]::GetAccessControl($directory)
    Assert-PrivateWindowsSecurity -Security $security
}

function New-WindowsRecoveryFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $security = New-RecoveryWindowsSecurity
    $stream = [IO.FileSystemAclExtensions]::Create(
        [IO.FileInfo]::new($Path), [IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security
    )
    try {
        Assert-PrivateWindowsSecurity -Security (
            [IO.FileSystemAclExtensions]::GetAccessControl($stream)
        )
        return $stream
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Initialize-PrivateRecoveryDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-RecoveryWindows) {
        Initialize-WindowsRecoveryDirectory -Path $Path
        return
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Unix) {
        throw "Recovery artifacts require Windows ACLs or POSIX owner-only permissions."
    }
    $mode = [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite -bor
        [IO.UnixFileMode]::UserExecute
    $directory = [IO.Directory]::CreateDirectory($Path, $mode)
    if ($null -ne $directory.LinkTarget) {
        throw "Recovery directory must not be a symbolic link: $Path"
    }
    $permissions = [int][IO.File]::GetUnixFileMode($Path)
    if (($permissions -band 63) -ne 0) {
        throw "Recovery directory must be owner-only (chmod 700): $Path"
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    Write-PrivateRecoveryText -Path $Path -Content ($Value | ConvertTo-Json -Depth 40)
}

function Write-PrivateRecoveryText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $parent = Split-Path -Parent $Path
    Initialize-PrivateRecoveryDirectory -Path $parent
    $temporary = Join-Path $parent (".recovery-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    $stream = $null
    try {
        if (Test-RecoveryWindows) {
            $stream = New-WindowsRecoveryFile -Path $temporary
        }
        else {
            $mode = [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite
            $options = [IO.FileStreamOptions]::new()
            $options.Mode = [IO.FileMode]::CreateNew
            $options.Access = [IO.FileAccess]::Write
            $options.Share = [IO.FileShare]::None
            $options.UnixCreateMode = $mode
            $stream = [IO.FileStream]::new($temporary, $options)
            [IO.File]::SetUnixFileMode($temporary, $mode)
            if ([IO.File]::GetUnixFileMode($temporary) -ne $mode) {
                throw "Unable to enforce owner-only artifact permissions: $temporary"
            }
        }
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        [IO.File]::Move($temporary, $Path, $true)
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}
