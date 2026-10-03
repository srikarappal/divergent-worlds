import json

import pytest
from support.image_fakes import LOCK, windows_config, windows_image, world_ca_pem

from rlworld.images import BuildError, windows_build_plan
from rlworld.images.windows import PACKER_QEMU_PLUGIN_VERSION, hcl_string, powershell_string, template_text


class PasswordSource:
    def __init__(self):
        self.issued = []

    def new_password(self):
        password = f"Rl1-secret-{len(self.issued)}"
        self.issued.append(password)
        return password


def plan_for(image=None, config=None, trusted_cas=None, passwords=None):
    source = passwords or PasswordSource()
    return windows_build_plan(image or windows_image(), config or windows_config(), LOCK, trusted_cas or {},
                              new_password=source.new_password)


def text(plan, path):
    return plan.files[path].decode("utf-8")


def test_packer_template_pins_the_plugin_iso_and_pool_hardware():
    template = text(plan_for(), "windows.pkr.hcl")
    assert f'version = "= {PACKER_QEMU_PLUGIN_VERSION}"' in template
    assert 'iso_url          = "https://iso.example/server2025.iso"' in template
    assert f'iso_checksum     = "sha256:{"8" * 64}"' in template
    assert 'machine_type     = "pc-q35-9.0"' in template
    assert 'qemuargs         = [["-cpu", "Cascadelake-Server-v5"]]' in template
    assert 'disk_interface   = "virtio"' in template and 'vm_name          = "win2025_office_v4.qcow2"' in template
    assert 'shutdown_command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\\\rlworld\\\\seal.ps1"' in (
        template)
    assert ('scripts = ["scripts/10-guest-tools.ps1", "scripts/20-downloads.ps1", "scripts/30-apps.ps1",'
            ' "scripts/50-settings.ps1"]') in template


def test_passwords_reach_packer_only_as_sensitive_variables():
    source = PasswordSource()
    plan = plan_for(passwords=source)
    template = text(plan, "windows.pkr.hcl")
    assert template.count("sensitive = true") == 2
    assert 'templatefile("autounattend.pkrtpl.xml", { admin_password = var.admin_password })' in template
    build = plan.commands[-1]
    assert build.argv == ("packer", "build", "-force", "-color=false", "windows.pkr.hcl")
    assert build.environment == {"PKR_VAR_admin_password": source.issued[0],
                                 "PKR_VAR_agent_password": source.issued[1]}
    assert all(password.encode() not in content for content in plan.files.values() for password in source.issued)
    assert "${admin_password}" in text(plan, "autounattend.pkrtpl.xml")
    assert "${agent_password}" in text(plan, "unattend-oobe.pkrtpl.xml")
    assert "PKR_VAR" not in json.dumps(plan.manifest)


def test_setup_answer_file_installs_the_pinned_edition_with_virtio_drivers():
    answer = text(plan_for(), "autounattend.pkrtpl.xml")
    assert "<Value>Windows Server 2025 SERVERDATACENTER</Value>" in answer
    assert "<Key>D764K-2NDRG-47T6Q-P8T8W-YP6DF</Key>" in answer
    assert "<Path>D:\\</Path>" in answer and "<Path>F:\\</Path>" in answer
    assert "<UILanguage>en-US</UILanguage>" in answer
    assert "Enable-PSRemoting -SkipNetworkProfileCheck -Force" in answer
    assert "<Username>Administrator</Username>" in answer and "<LogonCount>1</LogonCount>" in answer


def test_oobe_answer_file_autologs_on_a_standard_user():
    answer = text(plan_for(config=windows_config(user="trainee", time_zone="Pacific Standard Time")),
                  "unattend-oobe.pkrtpl.xml")
    assert "<Name>trainee</Name><Group>Users</Group>" in answer
    assert "<AutoLogon><Enabled>true</Enabled><Username>trainee</Username>" in answer
    assert "<TimeZone>Pacific Standard Time</TimeZone>" in answer
    assert "Administrators" not in answer


def test_template_values_cannot_inject_markup_or_template_sequences():
    assert template_text("A & B ${var.admin_password} %{ if }") == "A &amp; B $${var.admin_password} %%{ if }"
    assert hcl_string('C:\\x "${y}"') == '"C:\\\\x \\"$${y}\\""'
    assert powershell_string("it's") == "'it''s'"
    answer = text(plan_for(config=windows_config(image_name="Server ${evil} & co")), "autounattend.pkrtpl.xml")
    assert "<Value>Server $${evil} &amp; co</Value>" in answer


def test_virtio_drivers_come_from_the_locked_iso():
    plan = plan_for(config=windows_config(virtio_driver_os="2k22"))
    assert plan.downloads == {"downloads/virtio-win-0.1.266.iso": LOCK.require("virtio_win")}
    assert plan.commands[1].argv == ("bsdtar", "-xf", "downloads/virtio-win-0.1.266.iso", "-C", "virtio",
                                     "viostor/2k22/amd64", "NetKVM/2k22/amd64", "virtio-win-guest-tools.exe")
    assert plan.commands[2].argv == ("packer", "init", "windows.pkr.hcl")
    assert "Get-Volume -FileSystemLabel 'RLWORLD'" in text(plan, "scripts/10-guest-tools.ps1")


def test_downloads_are_checked_in_the_guest_before_install():
    plan = plan_for()
    downloads = text(plan, "scripts/20-downloads.ps1")
    odt_path = "C:\\rlworld\\downloads\\office_deployment_tool\\officedeploymenttool.exe"
    assert f"-Uri 'https://download.example/officedeploymenttool.exe' -OutFile '{odt_path}'" in downloads
    assert f"-ne '{'5' * 64}') {{ throw 'office_deployment_tool does not match" in downloads
    assert "rlworld-windows-helper.zip" in downloads and "MicrosoftEdgeEnterpriseX64.msi" in downloads
    apps = text(plan, "scripts/30-apps.ps1")
    assert f"Start-Process -FilePath '{odt_path}'" in apps
    assert "'/configure', 'C:\\rlworld\\office.xml'" in apps
    assert "'/i', 'C:\\rlworld\\downloads\\edge_131.0.2903.71\\MicrosoftEdgeEnterpriseX64.msi'" in apps
    assert "Expand-Archive -Path 'C:\\rlworld\\downloads\\rlworld_windows_helper\\rlworld-windows-helper.zip'" in apps
    assert "slmgr.vbs /skms kms.internal:1688" in apps and "OSPP.VBS' /sethst:kms.internal:1688" in apps


def test_kms_is_only_configured_when_a_host_is_given():
    apps = text(plan_for(windows_image(apps={"edge": "131.0.2903.71"}, ready_probe={"window": "Edge"}),
                         config=windows_config(kms_host=None)), "scripts/30-apps.ps1")
    assert "slmgr" not in apps and "OSPP" not in apps
    edge_only = text(plan_for(windows_image(apps={"edge": "131.0.2903.71"}, ready_probe={"window": "Edge"})),
                     "scripts/30-apps.ps1")
    assert "slmgr.vbs" in edge_only and "OSPP" not in edge_only


def test_office_installs_a_pinned_build_with_updates_off():
    plan = plan_for()
    office = text(plan, "payload/office.xml")
    assert '<Add OfficeClientEdition="64" Channel="PerpetualVL2024" Version="16.0.17932.20222">' in office
    assert '<Product ID="Excel2024Volume">' in office and '<Language ID="en-us" />' in office
    assert '<Updates Enabled="FALSE" />' in office and 'AcceptEULA="TRUE"' in office
    assert plan.manifest["office_build"] == "16.0.17932.20222"
    updating = text(plan_for(windows_image(hardening=[])), "payload/office.xml")
    assert '<Updates Enabled="TRUE" />' in updating
    with pytest.raises(BuildError, match="office label office_ltsc_2024 has no pinned build"):
        plan_for(config=windows_config(office_builds={}))
    assert "payload/office.xml" not in plan_for(windows_image(apps={"edge": "131.0.2903.71"},
                                                              ready_probe={"window": "Edge"})).files


def test_settings_write_machine_values_then_the_default_user_hive():
    settings = text(plan_for(), "scripts/50-settings.ps1")
    load = settings.index("reg.exe load HKU\\RLWORLD_DEFAULT C:\\Users\\Default\\NTUSER.DAT")
    unload = settings.index("reg.exe unload HKU\\RLWORLD_DEFAULT")
    assert settings.index("'HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU' /v 'NoAutoUpdate'") < load
    protected_view = settings.index("ProtectedView' /v 'DisableAttachementsInPV' /t REG_DWORD /d '1'")
    assert load < protected_view < unload
    assert load < settings.index("'HKU\\RLWORLD_DEFAULT\\Control Panel\\Desktop' /v 'LogPixels' /t REG_DWORD /d '120'")
    assert "/v 'Win8DpiScaling' /t REG_DWORD /d '1'" in settings
    assert "/v 'AppsUseLightTheme' /t REG_DWORD /d '0'" in settings
    assert "/v 'MinAnimate' /t REG_SZ /d '0'" in settings
    assert "if ($LASTEXITCODE -ne 0) { throw 'loading the default user hive failed' }" in settings
    assert "Uninstall-WindowsFeature -Name Windows-Defender" in settings
    assert settings.index("Set-Service -Name wuauserv -StartupType Disabled") > unload
    run_key = "'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run' /v 'rlworld-session' /t REG_SZ"
    assert run_key in settings and "C:\\rlworld\\logon.ps1" in settings


def test_unknown_hardening_fails_the_build():
    with pytest.raises(BuildError, match="hardening no_screen_savers does not apply to windows images"):
        plan_for(windows_image(hardening=["no_screen_savers"]))


def test_logon_starts_the_helper_and_the_ready_probe_window():
    plan = plan_for()
    logon = text(plan, "payload/logon.ps1")
    assert logon.startswith("Set-DisplayResolution -Width 1280 -Height 800 -Force\n")
    assert ("Start-Process -FilePath 'C:\\rlworld\\helper\\rlworld-windows-helper.exe'"
            " -ArgumentList '--launchers', 'C:\\rlworld\\launchers.json'") in logon
    assert logon.endswith("Start-Process -FilePath 'C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE'\n")
    launchers = json.loads(text(plan, "payload/launchers.json"))
    assert launchers["Edge"] == {"executable": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
                                 "arguments": ["--start-maximized"]}
    with pytest.raises(BuildError, match="ready probe window Calc"):
        plan_for(windows_image(ready_probe={"window": "Calc"}))


def test_seal_disables_winrm_and_removes_the_answer_file():
    seal = text(plan_for(), "payload/seal.ps1")
    assert "Set-Service -Name WinRM -StartupType Disabled" in seal
    assert "icacls.exe C:\\rlworld\\unattend-oobe.xml /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F" in seal
    assert "-Value 'del /f /q C:\\rlworld\\unattend-oobe.xml'" in seal
    assert seal.rstrip().endswith("sysprep.exe /generalize /oobe /shutdown /quiet"
                                  " /unattend:C:\\rlworld\\unattend-oobe.xml")
    assert seal.index("Remove-Item -Path 'C:\\rlworld\\downloads'") < seal.index("sysprep.exe")


def test_world_cas_go_into_the_machine_root_store():
    pem = world_ca_pem()
    plan = plan_for(trusted_cas={"world_ca": pem})
    assert plan.files["payload/ca/world_ca.crt"] == pem
    trust = text(plan, "scripts/40-trust.ps1")
    assert ("Import-Certificate -FilePath 'C:\\rlworld\\ca\\world_ca.crt' -CertStoreLocation"
            " 'Cert:\\LocalMachine\\Root'") in trust
    assert '"scripts/40-trust.ps1"' in text(plan, "windows.pkr.hcl")


def test_only_templated_qemu_images_are_built():
    with pytest.raises(BuildError, match="cloud_windows pools run the provider's images"):
        plan_for(windows_image(pool={"vmm": "cloud_windows", "cpu_model": "cloud"}))


def test_outputs_and_manifest_record_the_pinned_inputs():
    plan = plan_for()
    assert plan.outputs == {"disk": "output/win2025_office_v4.qcow2"}
    assert plan.manifest["machine"] == "pc-q35-9.0" and plan.manifest["cpu_model"] == "Cascadelake-Server-v5"
    assert plan.manifest["artifacts"] == {"office_deployment_tool": "5" * 64, "edge_131.0.2903.71": "6" * 64,
                                          "rlworld_windows_helper": "7" * 64, "windows_server_2025": "8" * 64,
                                          "virtio_win": "9" * 64}
    assert plan.manifest["launchers"]["Excel"].endswith("EXCEL.EXE")
