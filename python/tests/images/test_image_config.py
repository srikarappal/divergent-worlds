from pathlib import Path

import pytest
from pydantic import ValidationError
from support.image_fakes import LOCK, WORLD_TIME, linux_config, windows_config, world_ca_pem

from rlworld.images import (
    BuildError,
    load_artifact_lock,
    load_build_config,
    read_trusted_cas,
)
from rlworld.images.config import Artifact, locale_parts
from rlworld.images.recipes import (
    CHROMIUM_BASE_FLAGS,
    OFFICE_PRODUCTS,
    linux_app,
    windows_app,
)
from rlworld.network import create_certificate_authority, issue_server_certificate


def test_build_config_paths_resolve_next_to_the_config_file(tmp_path):
    config_path = tmp_path / "images" / "build.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "linux:\n"
        "  base_images: {ubuntu_noble: docker.io/library/ubuntu}\n"
        "  apt_sources: ['deb http://snapshot.ubuntu.com/ubuntu/20260301T000000Z noble main']\n"
        "  guest_agent_wheel: dist/rlworld-0.1.0-py3-none-any.whl\n"
        "  guest_agent_requirements: /opt/locks/guest.txt\n"
        "windows: {product_key: D764K-2NDRG-47T6Q-P8T8W-YP6DF}\n"
        "trusted_cas: {world_ca: cas/world_ca.pem}\n")
    config = load_build_config(config_path)
    assert config.linux.guest_agent_wheel == tmp_path / "images" / "dist" / "rlworld-0.1.0-py3-none-any.whl"
    assert config.linux.guest_agent_requirements == Path("/opt/locks/guest.txt")
    assert config.trusted_cas == {"world_ca": tmp_path / "images" / "cas" / "world_ca.pem"}
    assert config.windows.office_tool_artifact == "office_deployment_tool"


def test_apt_sources_must_be_dated_snapshots():
    with pytest.raises(ValidationError, match="dated snapshot mirrors"):
        linux_config(apt_sources=("deb http://archive.ubuntu.com/ubuntu noble main",))
    with pytest.raises(ValidationError):
        linux_config(apt_sources=())


def test_windows_config_pins_office_builds_and_checks_the_kms_host():
    with pytest.raises(ValidationError):
        windows_config(office_builds={"office_ltsc_2024": "latest"})
    with pytest.raises(ValidationError):
        windows_config(kms_host="kms.internal; calc.exe")
    with pytest.raises(ValidationError):
        windows_config(product_key="not-a-key")


def test_artifact_lock_requires_pins_and_names_downloads(tmp_path):
    lock_path = tmp_path / "artifacts.lock.yaml"
    kernel_pin = f"{{url: 'https://kernels.example/vmlinux', sha256: '{'4' * 64}'}}"
    lock_path.write_text(f"artifacts:\n  guest_kernel: {kernel_pin}\n")
    assert load_artifact_lock(lock_path).require("guest_kernel").sha256 == "4" * 64
    with pytest.raises(BuildError, match="chrome_131 is not in the lock"):
        LOCK.require("chrome_131")
    assert LOCK.require("firefox_128.5.0esr").file_name == "firefox-128.5.0esr.tar.xz"
    with pytest.raises(ValidationError):
        Artifact(url="http://insecure.example/file", sha256="1" * 64)
    with pytest.raises(ValidationError):
        Artifact(url="https://example.com/file", sha256="short")


def test_trusted_cas_must_be_ca_certificates(tmp_path):
    authority = create_certificate_authority("World CA", WORLD_TIME)
    leaf_pem, _ = issue_server_certificate(authority, ["crm.example.com"], WORLD_TIME)
    (tmp_path / "world.pem").write_bytes(world_ca_pem())
    (tmp_path / "leaf.pem").write_bytes(leaf_pem)
    (tmp_path / "junk.pem").write_bytes(b"not a certificate")
    certificates = read_trusted_cas({"world_ca": tmp_path / "world.pem"})
    assert certificates["world_ca"].startswith(b"-----BEGIN CERTIFICATE-----")
    with pytest.raises(BuildError) as raised:
        read_trusted_cas({"leaf": tmp_path / "leaf.pem", "junk": tmp_path / "junk.pem",
                          "world_ca": tmp_path / "world.pem"})
    assert len(raised.value.problems) == 2
    assert "trusted CA junk" in raised.value.problems[0] and "trusted CA leaf" in raised.value.problems[1]


def test_display_locales_look_like_en_us():
    assert locale_parts("de_DE") == ("de", "DE")
    with pytest.raises(BuildError, match="must look like en_US"):
        locale_parts("en-US")


def test_linux_recipes_pin_versions_and_expose_devtools_on_loopback():
    chrome = linux_app("chrome", "130.0.6723.58", "agent", ("--no-first-run",))
    assert chrome.artifact == "chrome_130.0.6723.58"
    assert chrome.launch.startswith("/opt/chrome/chrome " + " ".join(CHROMIUM_BASE_FLAGS))
    assert chrome.launch.endswith("--no-first-run --user-data-dir=/home/agent/.config/rlworld-chrome")
    calc = linux_app("libreoffice", "4:24.2.7-0ubuntu0.24.04.1", "agent")
    assert calc.packages == ("libreoffice-calc=4:24.2.7-0ubuntu0.24.04.1", "libreoffice-gtk3=4:24.2.7-0ubuntu0.24.04.1")
    assert calc.artifact is None and calc.window == "Calc"
    with pytest.raises(BuildError, match="no Linux recipe for app excel"):
        linux_app("excel", "office_ltsc_2024", "agent")
    with pytest.raises(BuildError, match="version label"):
        linux_app("chrome", "130; rm -rf /", "agent")


def test_windows_recipes_take_the_office_tool_from_the_config():
    excel = windows_app("excel", "office_ltsc_2024", windows_config(office_tool_artifact="odt_2025"))
    assert excel.artifact == "odt_2025"
    assert excel.office_product == OFFICE_PRODUCTS["office_ltsc_2024"]
    assert any("{download}" in line for line in excel.install)
    with pytest.raises(BuildError, match="no Office product for excel office_365"):
        windows_app("excel", "office_365", windows_config())
    with pytest.raises(BuildError, match="no Windows recipe for app firefox"):
        windows_app("firefox", "128.5.0esr", windows_config())
