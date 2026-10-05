from support.image_fakes import LOCK, linux_config, linux_image

from rlworld.images import linux_build_plan, linux_dev_build_plan
from rlworld.images.mail_client import DOVECOT_IMAP_CONFIG, profile_template, thunderbird_launcher
from rlworld.images.recipes import linux_app


def text(plan, path):
    return plan.files[path].decode("utf-8")


def test_thunderbird_installs_beside_dovecot_and_runs_the_world_mail_server():
    thunderbird = linux_app("thunderbird", "153.4.0esr", "agent")
    assert (thunderbird.window, thunderbird.artifact) == ("Thunderbird", "thunderbird_153.4.0esr")
    assert thunderbird.launch == "/usr/local/bin/rlworld-thunderbird" and "dovecot-imapd" in thunderbird.packages
    assert "systemctl mask dovecot.service dovecot.socket" in thunderbird.install
    assert [(service.name, service.command) for service in thunderbird.services] == [
        ("mail", "/opt/rlworld/bin/rlworld-mail-server")]
    assert set(thunderbird.files) == {"etc/rlworld/dovecot-imap.conf", "etc/rlworld/thunderbird/user.js",
                                      "usr/local/bin/rlworld-thunderbird", "etc/thunderbird/policies/policies.json"}


def test_the_profile_reads_the_world_mailbox_without_a_password_and_files_sent_mail_there():
    preferences = profile_template("agent")
    assert 'user_pref("mail.server.server1.port", 1143);' in preferences
    assert 'user_pref("mail.smtpserver.smtp1.port", 2525);' in preferences
    assert 'user_pref("mail.smtpserver.smtp1.authMethod", 1);' in preferences
    assert 'user_pref("mail.identity.id1.fcc_folder", "imap://agent@127.0.0.1/sent");' in preferences
    assert 'user_pref("mail.shell.checkDefaultClient", false);' in preferences
    assert "mail_location = maildir:~/world/mail/current:LAYOUT=fs:INBOX=~/world/mail/current/inbox" in (
        DOVECOT_IMAP_CONFIG)
    assert "special_use = \\Sent" in DOVECOT_IMAP_CONFIG
    launcher = thunderbird_launcher()
    assert '"$HOME/world/mail/owner.txt"' in launcher and "mail.identity.id1.useremail" in launcher
    assert launcher.endswith('exec /opt/thunderbird/thunderbird -profile "$profile" "$@"\n')


def test_images_with_thunderbird_enable_the_mail_service_and_list_it_in_the_menu():
    image = linux_image(desktop="xfce", apps={"libreoffice": "4:24.2.7-0ubuntu0.24.04.6",
                                              "thunderbird": "153.4.0esr"}, ready_probe={"window": "Calc"})
    plan = linux_build_plan(image, linux_config(), LOCK, {})
    dockerfile = text(plan, "context/Dockerfile")
    assert "rlworld-app-mail.service" in next(line for line in dockerfile.splitlines() if "systemctl enable" in line)
    assert "ExecStart=/opt/rlworld/bin/rlworld-mail-server" in text(
        plan, "context/rootfs/etc/systemd/system/rlworld-app-mail.service")
    assert "Exec=thunderbird %u" in text(plan, "context/rootfs/usr/share/applications/rlworld-thunderbird.desktop")
    init = text(linux_dev_build_plan(image, linux_config(), LOCK, {}), "context/dev/usr/local/bin/rlworld-dev-init")
    assert "exec /opt/rlworld/bin/rlworld-mail-server" in init
