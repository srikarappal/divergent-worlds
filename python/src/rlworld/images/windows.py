"""Windows images: Packer turns a pinned ISO into a qcow2 with autounattend, virtio-win and sysprep (DESIGN.md
sections 6.1 to 6.3).

The build VM pins the pool's machine type and CPU model so the image restores
on that pool. Windows Setup loads the virtio storage and network drivers from
a CD, then WinRM carries the provisioners: guest tools, checked downloads,
Office through the deployment tool at a pinned build, Edge, the in session
helper, world CAs, hardening and display settings. Per user settings go into
the default user hive because the autologon user only exists after sysprep;
that user is a standard user, so the policy never holds admin rights. The
passwords are random per build and reach Packer only as sensitive variables;
the answer file holding them is locked to admins, then deleted once setup
completes. WinRM is disabled before sysprep.
"""

import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from xml.sax.saxutils import escape

from rlworld.images.config import (
    Artifact,
    ArtifactLock,
    BuildCommand,
    BuildError,
    BuildPlan,
    WindowsBuildConfig,
    locale_parts,
)
from rlworld.images.recipes import DOWNLOAD_PLACEHOLDER, WindowsApp, windows_app
from rlworld.specs.models import DisplaySpec, ImageSpec

PACKER_QEMU_PLUGIN_VERSION = "1.1.0"
MEDIA_LABEL = "RLWORLD"
GUEST_ROOT = "C:\\rlworld"
GUEST_DOWNLOADS = f"{GUEST_ROOT}\\downloads"
HELPER_DIRECTORY = f"{GUEST_ROOT}\\helper"
HELPER_EXECUTABLE = f"{HELPER_DIRECTORY}\\rlworld-windows-helper.exe"
LAUNCHERS_PATH = f"{GUEST_ROOT}\\launchers.json"
OOBE_ANSWER_FILE = f"{GUEST_ROOT}\\unattend-oobe.xml"
DEFAULT_USER_HIVE = "HKU\\RLWORLD_DEFAULT"
LOGON_COMMAND = f"powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File {GUEST_ROOT}\\logon.ps1"
SHUTDOWN_COMMAND = f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {GUEST_ROOT}\\seal.ps1"
COMPONENT_ATTRIBUTES = ('processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral"'
                        ' versionScope="nonSxS"')
DRIVER_LETTERS = ("D", "E", "F")
VIRTIO_DRIVERS = ("viostor", "NetKVM")
VIRTIO_GUEST_TOOLS = "virtio-win-guest-tools.exe"
SCRIPT_PROLOGUE = "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n"
ENABLE_WINRM = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "'
                "Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private;"
                " Enable-PSRemoting -SkipNetworkProfileCheck -Force;"
                " Set-Item -Path WSMan:\\localhost\\Service\\AllowUnencrypted -Value $true;"
                ' Set-Item -Path WSMan:\\localhost\\Service\\Auth\\Basic -Value $true"')


@dataclass(frozen=True)
class RegistryValue:
    """User values land in the default user hive, which every new profile copies."""

    hive: Literal["machine", "user"]
    key: str
    name: str
    data: int | str

    @property
    def kind(self) -> str:
        return "REG_DWORD" if isinstance(self.data, int) else "REG_SZ"


@dataclass(frozen=True)
class WindowsHardening:
    registry: tuple[RegistryValue, ...] = ()
    commands: tuple[str, ...] = ()


def machine(key: str, name: str, data: int | str) -> RegistryValue:
    return RegistryValue("machine", key, name, data)


def user(key: str, name: str, data: int | str) -> RegistryValue:
    return RegistryValue("user", key, name, data)


EDGE_POLICIES = "SOFTWARE\\Policies\\Microsoft\\Edge"
OFFICE_UPDATE_POLICIES = "SOFTWARE\\Policies\\Microsoft\\office\\16.0\\common\\officeupdate"
OFFICE_USER = "Software\\Microsoft\\Office\\16.0"
PROTECTED_VIEW_POLICIES = "Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security\\ProtectedView"
WINDOWS_HARDENING = {
    "no_auto_update": WindowsHardening(
        registry=(machine("SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "NoAutoUpdate", 1),
                  machine(OFFICE_UPDATE_POLICIES, "enableautomaticupdates", 0),
                  machine(OFFICE_UPDATE_POLICIES, "hideenabledisableupdates", 1),
                  machine("SOFTWARE\\Policies\\Microsoft\\EdgeUpdate", "UpdateDefault", 0),
                  machine("SOFTWARE\\Policies\\Microsoft\\EdgeUpdate", "AutoUpdateCheckPeriodMinutes", 0)),
        commands=("Stop-Service -Name wuauserv -Force", "Set-Service -Name wuauserv -StartupType Disabled",
                  "Get-Service -Name 'edgeupdate*' | Set-Service -StartupType Disabled",
                  "Get-ScheduledTask | Where-Object TaskName -like 'MicrosoftEdgeUpdate*' | Disable-ScheduledTask"
                  " | Out-Null")),
    "no_defender_scan": WindowsHardening(commands=("Uninstall-WindowsFeature -Name Windows-Defender | Out-Null",)),
    "no_search_indexing": WindowsHardening(
        commands=("Get-Service -Name 'WSearch*' | Stop-Service -Force -PassThru | Set-Service -StartupType Disabled",)),
    "no_telemetry": WindowsHardening(
        registry=(machine("SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 0),
                  machine(EDGE_POLICIES, "MetricsReportingEnabled", 0),
                  machine(EDGE_POLICIES, "DiagnosticData", 0),
                  user("Software\\Policies\\Microsoft\\office\\common\\clienttelemetry", "sendtelemetry", 3)),
        commands=("Stop-Service -Name DiagTrack -Force", "Set-Service -Name DiagTrack -StartupType Disabled")),
    "no_first_run": WindowsHardening(
        registry=(machine("SOFTWARE\\Policies\\Microsoft\\Windows\\Server\\ServerManager", "DoNotOpenAtLogon", 1),
                  machine("SOFTWARE\\Microsoft\\ServerManager", "DoNotPopWACConsoleAtSMLaunch", 1),
                  machine("SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System",
                          "EnableFirstLogonAnimation", 0),
                  machine("SOFTWARE\\Policies\\Microsoft\\Windows\\OOBE", "DisablePrivacyExperience", 1),
                  machine(EDGE_POLICIES, "HideFirstRunExperience", 1),
                  user(f"{OFFICE_USER}\\Common\\General", "ShownFirstRunOptin", 1),
                  user(f"{OFFICE_USER}\\Registration", "AcceptAllEulas", 1),
                  user(f"{OFFICE_USER}\\Excel\\Options", "DisableBootToOfficeStart", 1)),
        commands=("New-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Network\\NewNetworkWindowOff' -Force"
                  " | Out-Null",)),
    "no_protected_view": WindowsHardening(
        registry=(user(PROTECTED_VIEW_POLICIES, "DisableInternetFilesInPV", 1),
                  user(PROTECTED_VIEW_POLICIES, "DisableAttachementsInPV", 1),
                  user(PROTECTED_VIEW_POLICIES, "DisableUnsafeLocationsInPV", 1))),
    "no_activation_prompts": WindowsHardening(
        registry=(machine("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\SoftwareProtectionPlatform\\Activation",
                          "NotificationDisabled", 1),)),
    "no_animations": WindowsHardening(
        registry=(user("Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "0"),
                  user("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VisualEffects", "VisualFXSetting", 2),
                  user("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "TaskbarAnimations", 0),
                  user("Software\\Microsoft\\Windows\\DWM", "EnableAeroPeek", 0),
                  user(f"{OFFICE_USER}\\Common\\Graphics", "DisableAnimations", 1))),
}


def random_password() -> str:
    """Upper, lower, digit and symbol up front, so Windows complexity rules always pass."""
    return f"Rl1-{secrets.token_urlsafe(18)}"


def windows_hardening(names: Sequence[str]) -> list[WindowsHardening]:
    unknown = [name for name in names if name not in WINDOWS_HARDENING]
    if unknown:
        known = ", ".join(WINDOWS_HARDENING)
        raise BuildError([f"hardening {name} does not apply to windows images; known: {known}" for name in unknown])
    return [WINDOWS_HARDENING[name] for name in names]


def powershell_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def hcl_string(text: str) -> str:
    """A quoted HCL string whose content is never read as a template."""
    return json.dumps(text).replace("${", "$${").replace("%{", "%%{")


def template_text(text: str) -> str:
    """XML text inside a Packer template, so neither markup nor template sequences slip in."""
    return escape(text).replace("${", "$${").replace("%{", "%%{")


def checked_native(command: str, action: str) -> str:
    """Native tools report failure through their exit code, which PowerShell does not turn into an error."""
    return f"{command} | Out-Null\nif ($LASTEXITCODE -ne 0) {{ throw {powershell_string(f'{action} failed')} }}\n"


def registry_line(value: RegistryValue) -> str:
    key = f"{'HKLM' if value.hive == 'machine' else DEFAULT_USER_HIVE}\\{value.key}"
    return checked_native(f"reg.exe add {powershell_string(key)} /v {powershell_string(value.name)} /t {value.kind}"
                          f" /d {powershell_string(str(value.data))} /f", f"setting {key} {value.name}")


def display_values(display: DisplaySpec) -> list[RegistryValue]:
    light = 0 if display.theme == "dark" else 1
    personalize = "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
    return [user("Control Panel\\Desktop", "LogPixels", display.dpi),
            user("Control Panel\\Desktop", "Win8DpiScaling", 0 if display.dpi == 96 else 1),
            user(personalize, "AppsUseLightTheme", light), user(personalize, "SystemUsesLightTheme", light)]


def guest_download_path(name: str, artifact: Artifact) -> str:
    return f"{GUEST_DOWNLOADS}\\{name}\\{artifact.file_name}"


def guest_tools_script() -> str:
    return (SCRIPT_PROLOGUE
            + f"$media = Get-Volume -FileSystemLabel '{MEDIA_LABEL}'\n"
            + '$tools = Get-ChildItem -Path "$($media.DriveLetter):\\" -Recurse'
            + f" -Filter '{VIRTIO_GUEST_TOOLS}' | Select-Object -First 1\n"
            + f"if ($null -eq $tools) {{ throw '{VIRTIO_GUEST_TOOLS} is not on the {MEDIA_LABEL} media' }}\n"
            + "$process = Start-Process -FilePath $tools.FullName -ArgumentList '/install', '/quiet', '/norestart'"
            + " -Wait -PassThru\n"
            + 'if ($process.ExitCode -notin 0, 3010) { throw "guest tools exited with $($process.ExitCode)" }\n')


def downloads_script(downloads: Mapping[str, Artifact]) -> str:
    lines = [SCRIPT_PROLOGUE]
    for name, artifact in downloads.items():
        path = guest_download_path(name, artifact)
        lines += [f"New-Item -ItemType Directory -Path {powershell_string(f'{GUEST_DOWNLOADS}\\{name}')} -Force"
                  " | Out-Null\n",
                  f"Invoke-WebRequest -UseBasicParsing -Uri {powershell_string(artifact.url)}"
                  f" -OutFile {powershell_string(path)}\n",
                  f"if ((Get-FileHash -Algorithm SHA256 -Path {powershell_string(path)}).Hash.ToLowerInvariant()"
                  f" -ne '{artifact.sha256}') {{ throw '{name} does not match its sha256 in the artifact lock' }}\n"]
    return "".join(lines)


def apps_script(apps: Mapping[str, WindowsApp], downloads: Mapping[str, Artifact], config: WindowsBuildConfig) -> str:
    lines = [SCRIPT_PROLOGUE]
    for recipe in apps.values():
        download = guest_download_path(recipe.artifact, downloads[recipe.artifact]).replace("'", "''")
        lines += [f"{line.replace(DOWNLOAD_PLACEHOLDER, download)}\n" for line in recipe.install]
    helper_archive = guest_download_path(config.helper_artifact, downloads[config.helper_artifact])
    lines.append(f"Expand-Archive -Path {powershell_string(helper_archive)}"
                 f" -DestinationPath {powershell_string(HELPER_DIRECTORY)} -Force\n")
    if config.kms_host:
        lines.append(checked_native(f"cscript.exe //B C:\\Windows\\System32\\slmgr.vbs /skms {config.kms_host}",
                                    "setting the Windows KMS host"))
        if any(recipe.office_product for recipe in apps.values()):
            lines.append(checked_native("cscript.exe //B 'C:\\Program Files\\Microsoft Office\\Office16\\OSPP.VBS'"
                                        f" /sethst:{config.kms_host}", "setting the Office KMS host"))
    return "".join(lines)


def trust_script(ca_names: Sequence[str]) -> str:
    return SCRIPT_PROLOGUE + "".join(
        f"Import-Certificate -FilePath {powershell_string(f'{GUEST_ROOT}\\ca\\{name}.crt')}"
        " -CertStoreLocation 'Cert:\\LocalMachine\\Root' | Out-Null\n" for name in ca_names)


def settings_script(values: Sequence[RegistryValue], commands: Sequence[str]) -> str:
    """Machine values, then user values inside the loaded default user hive, then hardening commands."""
    hive_file = "C:\\Users\\Default\\NTUSER.DAT"
    return (SCRIPT_PROLOGUE
            + "".join(registry_line(value) for value in values if value.hive == "machine")
            + checked_native(f"reg.exe load {DEFAULT_USER_HIVE} {hive_file}", "loading the default user hive")
            + "".join(registry_line(value) for value in values if value.hive == "user")
            + checked_native(f"reg.exe unload {DEFAULT_USER_HIVE}", "unloading the default user hive")
            + "".join(f"{command}\n" for command in commands))


def start_process_line(recipe: WindowsApp) -> str:
    arguments = (" -ArgumentList " + ", ".join(powershell_string(argument) for argument in recipe.arguments)
                 if recipe.arguments else "")
    return f"Start-Process -FilePath {powershell_string(recipe.executable)}{arguments}\n"


def logon_script(display: DisplaySpec, probe: WindowsApp | None) -> str:
    """Runs in the autologon session: screen size, the in session helper, then the ready probe's window."""
    return (f"Set-DisplayResolution -Width {display.width} -Height {display.height} -Force\n"
            f"Start-Process -FilePath {powershell_string(HELPER_EXECUTABLE)}"
            f" -ArgumentList '--launchers', {powershell_string(LAUNCHERS_PATH)}\n"
            + (start_process_line(probe) if probe else ""))


def seal_script() -> str:
    """Packer's shutdown command: no remote management, no downloads and no readable passwords survive sysprep."""
    setup_complete = "C:\\Windows\\Setup\\Scripts\\SetupComplete.cmd"
    return (SCRIPT_PROLOGUE
            + "Set-Service -Name WinRM -StartupType Disabled\n"
            + "Get-NetFirewallRule -DisplayGroup 'Windows Remote Management' | Disable-NetFirewallRule\n"
            + f"Remove-Item -Path {powershell_string(GUEST_DOWNLOADS)} -Recurse -Force\n"
            + checked_native(f"icacls.exe {OOBE_ANSWER_FILE} /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F",
                             "locking the answer file to admins")
            + "New-Item -ItemType Directory -Path 'C:\\Windows\\Setup\\Scripts' -Force | Out-Null\n"
            + f"Set-Content -Path {powershell_string(setup_complete)}"
            + f" -Value {powershell_string(f'del /f /q {OOBE_ANSWER_FILE}')} -Encoding Ascii\n"
            + "& C:\\Windows\\System32\\Sysprep\\sysprep.exe /generalize /oobe /shutdown /quiet"
            + f" /unattend:{OOBE_ANSWER_FILE}\n")


def office_configuration(product: tuple[str, str], build: str, language: str, updates: bool) -> str:
    """The deployment tool's configuration: one volume licensed product at a pinned build, activated against KMS."""
    product_id, channel = product
    return ("<Configuration>\n"
            f'  <Add OfficeClientEdition="64" Channel="{channel}" Version="{build}">\n'
            f'    <Product ID="{product_id}">\n'
            f'      <Language ID="{language}" />\n'
            "    </Product>\n"
            "  </Add>\n"
            '  <Property Name="AUTOACTIVATE" Value="1" />\n'
            '  <Property Name="FORCEAPPSHUTDOWN" Value="TRUE" />\n'
            f'  <Updates Enabled="{"TRUE" if updates else "FALSE"}" />\n'
            '  <Display Level="None" AcceptEULA="TRUE" />\n'
            "</Configuration>\n")


def component(name: str, body: str) -> str:
    return f'    <component name="{name}" {COMPONENT_ATTRIBUTES}>\n{body}    </component>\n'


def international(language_tag: str, windows_pe: bool) -> str:
    setup_language = (f"      <SetupUILanguage><UILanguage>{language_tag}</UILanguage></SetupUILanguage>\n"
                      if windows_pe else "")
    return (f"{setup_language}      <InputLocale>{language_tag}</InputLocale>\n"
            f"      <SystemLocale>{language_tag}</SystemLocale>\n      <UILanguage>{language_tag}</UILanguage>\n"
            f"      <UserLocale>{language_tag}</UserLocale>\n")


def password(element: str, variable: str) -> str:
    """The value is a Packer template variable, filled in only when Packer renders the file."""
    return f"<{element}><Value>${{{variable}}}</Value><PlainText>true</PlainText></{element}>"


def answer_file(passes: Mapping[str, str]) -> str:
    settings = "".join(f'  <settings pass="{name}">\n{body}  </settings>\n' for name, body in passes.items())
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<unattend xmlns="urn:schemas-microsoft-com:unattend"'
            ' xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">\n'
            f"{settings}</unattend>\n")


def oobe_settings() -> str:
    hidden = ("HideEULAPage", "HideLocalAccountScreen", "HideOEMRegistrationScreen", "HideOnlineAccountScreens",
              "HideWirelessSetupInOOBE")
    return ("      <OOBE>\n" + "".join(f"        <{name}>true</{name}>\n" for name in hidden)
            + "        <ProtectYourPC>3</ProtectYourPC>\n      </OOBE>\n")


def specialize(config: WindowsBuildConfig) -> str:
    return component("Microsoft-Windows-Shell-Setup", "      <ComputerName>RLWORLD</ComputerName>\n"
                     f"      <TimeZone>{template_text(config.time_zone)}</TimeZone>\n")


def setup_answer_file(config: WindowsBuildConfig, language_tag: str) -> str:
    """Windows Setup: virtio drivers from any CD, one NTFS partition, the pinned edition, then WinRM for the build."""
    driver_paths = "".join(f'        <PathAndCredentials wcm:action="add" wcm:keyValue="{index}">'
                           f"<Path>{letter}:\\</Path></PathAndCredentials>\n"
                           for index, letter in enumerate(DRIVER_LETTERS, start=1))
    setup = ("      <DiskConfiguration>\n"
             '        <Disk wcm:action="add">\n'
             "          <DiskID>0</DiskID>\n"
             "          <WillWipeDisk>true</WillWipeDisk>\n"
             '          <CreatePartitions><CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type>'
             "<Extend>true</Extend></CreatePartition></CreatePartitions>\n"
             '          <ModifyPartitions><ModifyPartition wcm:action="add"><Order>1</Order>'
             "<PartitionID>1</PartitionID><Active>true</Active><Format>NTFS</Format><Label>Windows</Label>"
             "<Letter>C</Letter></ModifyPartition></ModifyPartitions>\n"
             "        </Disk>\n"
             "      </DiskConfiguration>\n"
             "      <ImageInstall>\n"
             "        <OSImage>\n"
             '          <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/NAME</Key>'
             f"<Value>{template_text(config.image_name)}</Value></MetaData></InstallFrom>\n"
             "          <InstallTo><DiskID>0</DiskID><PartitionID>1</PartitionID></InstallTo>\n"
             "        </OSImage>\n"
             "      </ImageInstall>\n"
             "      <UserData>\n"
             "        <AcceptEula>true</AcceptEula>\n"
             f"        <ProductKey><Key>{config.product_key}</Key><WillShowUI>OnError</WillShowUI></ProductKey>\n"
             "      </UserData>\n")
    shell_setup = (oobe_settings()
                   + f"      <UserAccounts>{password('AdministratorPassword', 'admin_password')}</UserAccounts>\n"
                   + "      <AutoLogon><Enabled>true</Enabled><Username>Administrator</Username>"
                   + f"{password('Password', 'admin_password')}<LogonCount>1</LogonCount></AutoLogon>\n"
                   + '      <FirstLogonCommands><SynchronousCommand wcm:action="add"><Order>1</Order>'
                   + f"<CommandLine>{template_text(ENABLE_WINRM)}</CommandLine>"
                   + "<Description>WinRM for the image build</Description></SynchronousCommand></FirstLogonCommands>\n")
    return answer_file({
        "windowsPE": (component("Microsoft-Windows-International-Core-WinPE", international(language_tag, True))
                      + component("Microsoft-Windows-PnpCustomizationsWinPE",
                                  f"      <DriverPaths>\n{driver_paths}      </DriverPaths>\n")
                      + component("Microsoft-Windows-Setup", setup)),
        "specialize": specialize(config),
        "oobeSystem": (component("Microsoft-Windows-International-Core", international(language_tag, False))
                       + component("Microsoft-Windows-Shell-Setup", shell_setup)),
    })


def oobe_answer_file(config: WindowsBuildConfig, language_tag: str) -> str:
    """After sysprep: the standard autologon user whose session runs the helper and the apps."""
    account = template_text(config.user)
    shell_setup = (oobe_settings()
                   + f"      <UserAccounts>{password('AdministratorPassword', 'admin_password')}"
                   + '<LocalAccounts><LocalAccount wcm:action="add">'
                   + f"<Name>{account}</Name><Group>Users</Group>{password('Password', 'agent_password')}"
                   + "</LocalAccount></LocalAccounts></UserAccounts>\n"
                   + f"      <AutoLogon><Enabled>true</Enabled><Username>{account}</Username>"
                   + f"{password('Password', 'agent_password')}<LogonCount>9999999</LogonCount></AutoLogon>\n")
    return answer_file({
        "specialize": specialize(config),
        "oobeSystem": (component("Microsoft-Windows-International-Core", international(language_tag, False))
                       + component("Microsoft-Windows-Shell-Setup", shell_setup)),
    })


def packer_template(image: ImageSpec, config: WindowsBuildConfig, iso: Artifact, scripts: Sequence[str]) -> str:
    script_paths = ", ".join(hcl_string(f"scripts/{name}") for name in scripts)
    return f"""packer {{
  required_plugins {{
    qemu = {{
      source  = "github.com/hashicorp/qemu"
      version = "= {PACKER_QEMU_PLUGIN_VERSION}"
    }}
  }}
}}

variable "admin_password" {{
  type      = string
  sensitive = true
}}

variable "agent_password" {{
  type      = string
  sensitive = true
}}

source "qemu" "windows" {{
  vm_name          = {hcl_string(f"{image.id}.qcow2")}
  output_directory = "output"
  iso_url          = {hcl_string(iso.url)}
  iso_checksum     = {hcl_string(f"sha256:{iso.sha256}")}
  machine_type     = {hcl_string(image.pool.machine or "")}
  accelerator      = "kvm"
  qemuargs         = [["-cpu", {hcl_string(image.pool.cpu_model)}]]
  cpus             = {config.vcpus}
  memory           = {config.memory_mib}
  disk_size        = "{config.disk_size_mib}M"
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  format           = "qcow2"
  headless         = true
  boot_wait        = "2s"
  boot_command     = ["<spacebar>"]
  cd_label         = "{MEDIA_LABEL}"
  cd_files         = ["virtio/"]
  cd_content = {{
    "autounattend.xml" = templatefile("autounattend.pkrtpl.xml", {{ admin_password = var.admin_password }})
  }}
  communicator     = "winrm"
  winrm_username   = "Administrator"
  winrm_password   = var.admin_password
  winrm_timeout    = "4h"
  shutdown_command = {hcl_string(SHUTDOWN_COMMAND)}
  shutdown_timeout = "1h"
}}

build {{
  sources = ["source.qemu.windows"]

  provisioner "file" {{
    source      = "payload/"
    destination = {hcl_string(GUEST_ROOT)}
  }}

  provisioner "file" {{
    content = templatefile("unattend-oobe.pkrtpl.xml", {{
      admin_password = var.admin_password
      agent_password = var.agent_password
    }})
    destination = {hcl_string(OOBE_ANSWER_FILE)}
  }}

  provisioner "powershell" {{
    scripts = [{script_paths}]
  }}

  provisioner "windows-restart" {{
    restart_timeout = "30m"
  }}
}}
"""


def windows_build_plan(image: ImageSpec, config: WindowsBuildConfig, lock: ArtifactLock,
                       trusted_cas: Mapping[str, bytes],
                       new_password: Callable[[], str] = random_password) -> BuildPlan:
    if image.pool.vmm != "qemu_templated":
        raise BuildError([f"{image.pool.vmm} pools run the provider's images; only qemu_templated images are built"])
    language, territory = locale_parts(image.display.locale)
    language_tag = f"{language}-{territory}"
    hardening = windows_hardening(image.hardening)
    apps = {app: windows_app(app, label, config) for app, label in image.apps.items()}
    probe_window = image.ready_probe.window
    probes = [recipe for recipe in apps.values() if recipe.window == probe_window]
    if probe_window is not None and not probes:
        raise BuildError([f"ready probe window {probe_window} is not opened by any app in the image"])
    downloads = {recipe.artifact: lock.require(recipe.artifact) for recipe in apps.values()}
    downloads[config.helper_artifact] = lock.require(config.helper_artifact)
    iso = lock.require(config.iso_artifact)
    virtio = lock.require(config.virtio_artifact)
    payload = {
        "logon.ps1": logon_script(image.display, probes[0] if probes else None),
        "seal.ps1": seal_script(),
        "launchers.json": json.dumps({recipe.window: {"executable": recipe.executable,
                                                      "arguments": list(recipe.arguments)}
                                      for recipe in apps.values()}, indent=2, sort_keys=True) + "\n",
    }
    office_build = None
    for app, recipe in apps.items():
        if recipe.office_product is not None:
            if image.apps[app] not in config.office_builds:
                raise BuildError([f"office label {image.apps[app]} has no pinned build in office_builds"])
            office_build = config.office_builds[image.apps[app]]
            payload["office.xml"] = office_configuration(recipe.office_product, office_build, language_tag.lower(),
                                                         updates="no_auto_update" not in image.hardening)
    scripts = {"10-guest-tools.ps1": guest_tools_script(), "20-downloads.ps1": downloads_script(downloads),
               "30-apps.ps1": apps_script(apps, downloads, config)}
    if trusted_cas:
        scripts["40-trust.ps1"] = trust_script(sorted(trusted_cas))
    registry = [value for step in hardening for value in step.registry]
    registry += display_values(image.display)
    registry.append(machine("SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run", "rlworld-session", LOGON_COMMAND))
    scripts["50-settings.ps1"] = settings_script(registry, [command for step in hardening for command in step.commands])
    files = {
        "windows.pkr.hcl": packer_template(image, config, iso, list(scripts)),
        "autounattend.pkrtpl.xml": setup_answer_file(config, language_tag),
        "unattend-oobe.pkrtpl.xml": oobe_answer_file(config, language_tag),
        **{f"scripts/{name}": script for name, script in scripts.items()},
        **{f"payload/{name}": content for name, content in payload.items()},
    }
    encoded = {path: content.encode("utf-8") for path, content in files.items()}
    encoded.update({f"payload/ca/{name}.crt": pem for name, pem in trusted_cas.items()})
    driver_members = [f"{driver}/{config.virtio_driver_os}/amd64" for driver in VIRTIO_DRIVERS]
    artifacts = {**downloads, config.iso_artifact: iso, config.virtio_artifact: virtio}
    return BuildPlan(
        files=encoded,
        copies={},
        downloads={f"downloads/{virtio.file_name}": virtio},
        commands=(
            BuildCommand(("mkdir", "-p", "virtio")),
            BuildCommand(("bsdtar", "-xf", f"downloads/{virtio.file_name}", "-C", "virtio", *driver_members,
                          VIRTIO_GUEST_TOOLS)),
            BuildCommand(("packer", "init", "windows.pkr.hcl")),
            BuildCommand(("packer", "build", "-force", "-color=false", "windows.pkr.hcl"),
                         environment={"PKR_VAR_admin_password": new_password(),
                                      "PKR_VAR_agent_password": new_password()}),
        ),
        outputs={"disk": f"output/{image.id}.qcow2"},
        manifest={"machine": image.pool.machine, "cpu_model": image.pool.cpu_model,
                  "packer_qemu_plugin": PACKER_QEMU_PLUGIN_VERSION, "office_build": office_build,
                  "user": config.user, "launchers": {recipe.window: recipe.executable for recipe in apps.values()},
                  "artifacts": {name: artifact.sha256 for name, artifact in artifacts.items()}},
    )
