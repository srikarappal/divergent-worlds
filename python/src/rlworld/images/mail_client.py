"""Thunderbird on the world's mail (DESIGN.md sections 6.2 and 6.4).

The image carries a profile that is already set up: one IMAP account on the
guest mail server, which greets preauthenticated, one SMTP server without
authentication and every first run screen switched off. The launcher writes the
agent persona's name and address into the profile from the materialized world
each time Thunderbird starts, so it has to start after task setup, as the
policy or a task opens it. Drafts go to the world mailbox's drafts folder and
the mail server files sent mail in its sent folder, so Thunderbird keeps no copy.
"""

from rlworld.guest.mail_server import DEFAULT_IMAP_PORT, DEFAULT_SMTP_PORT, DOVECOT_CONFIG
from rlworld.worlds.integrations import CURRENT_MAILBOX, USER_WORLD_FOLDER

THUNDERBIRD_LAUNCHER = "/usr/local/bin/rlworld-thunderbird"
THUNDERBIRD_ICON = "/opt/thunderbird/chrome/icons/default/default128.png"
PROFILE_TEMPLATE = "/etc/rlworld/thunderbird/user.js"
MAIL_ROOT = f"$HOME/{USER_WORLD_FOLDER}/mail"
WORLD_MAILBOX = f"~/{USER_WORLD_FOLDER}/mail/{CURRENT_MAILBOX}"

DOVECOT_IMAP_CONFIG = f"""# The world mailbox of the desktop user, for the imap process the mail server starts.
mail_location = maildir:{WORLD_MAILBOX}:LAYOUT=fs:INBOX={WORLD_MAILBOX}/inbox
namespace inbox {{
  inbox = yes
  separator = /
  mailbox sent {{
    auto = subscribe
    special_use = \\Sent
  }}
  mailbox drafts {{
    auto = subscribe
    special_use = \\Drafts
  }}
  mailbox trash {{
    auto = subscribe
    special_use = \\Trash
  }}
}}
"""

COMMON_PREFERENCES = {
    "mail.accountmanager.accounts": "account1,account2",
    "mail.accountmanager.defaultaccount": "account1",
    "mail.accountmanager.localfoldersserver": "server2",
    "mail.account.account1.server": "server1",
    "mail.account.account1.identities": "id1",
    "mail.account.account2.server": "server2",
    "mail.server.server1.type": "imap",
    "mail.server.server1.hostname": "127.0.0.1",
    "mail.server.server1.port": DEFAULT_IMAP_PORT,
    "mail.server.server1.socketType": 0,
    "mail.server.server1.authMethod": 3,
    "mail.server.server1.name": "Work",
    "mail.server.server1.login_at_startup": True,
    "mail.server.server1.check_new_mail": True,
    "mail.server.server1.check_time": 1,
    "mail.server.server1.use_idle": True,
    "mail.server.server2.type": "none",
    "mail.server.server2.hostname": "Local Folders",
    "mail.server.server2.userName": "nobody",
    "mail.server.server2.name": "Local Folders",
    "mail.identity.id1.smtpServer": "smtp1",
    "mail.identity.id1.fcc": False,
    "mail.identity.id1.drafts_folder_picker_mode": "1",
    "mail.identity.id1.valid": True,
    "mail.smtpservers": "smtp1",
    "mail.smtp.defaultserver": "smtp1",
    "mail.smtpserver.smtp1.hostname": "127.0.0.1",
    "mail.smtpserver.smtp1.port": DEFAULT_SMTP_PORT,
    "mail.smtpserver.smtp1.authMethod": 1,
    "mail.smtpserver.smtp1.try_ssl": 0,
    "mail.smtpserver.smtp1.description": "Work",
    "mail.shell.checkDefaultClient": False,
    "mailnews.start_page.enabled": False,
    "mail.rights.version": 1,
    "mail.provider.enabled": False,
    "mail.instrumentation.askUser": False,
    "mail.biff.show_alert": False,
    "mail.biff.play_sound": False,
    "mail.spotlight.firstRunDone": True,
    "app.donation.eoy.version.viewed": 999,
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "ui.prefersReducedMotion": 1,
}

THUNDERBIRD_POLICIES = '{\n  "policies": {\n    "DisableAppUpdate": true,\n    "DisableTelemetry": true\n  }\n}\n'


def preference_value(value: bool | int | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, int) else f'"{value}"'


def profile_template(user: str) -> str:
    """The account wiring and first run settings; folders are named by the IMAP user's folder URIs."""
    preferences = {**COMMON_PREFERENCES, "mail.server.server1.userName": user,
                   "mail.identity.id1.draft_folder": f"imap://{user}@127.0.0.1/drafts"}
    return "".join(f'user_pref("{name}", {preference_value(value)});\n' for name, value in preferences.items())


def thunderbird_launcher() -> str:
    """Starts Thunderbird on the image's profile with the agent persona as its identity."""
    return ("#!/bin/sh\nset -eu\n"
            'profile="$HOME/.thunderbird/rlworld"\n'
            'mkdir -p "$profile"\n'
            f'cp {PROFILE_TEMPLATE} "$profile/user.js"\n'
            f'if [ -f "{MAIL_ROOT}/owner.txt" ]; then\n'
            f'  address=$(sed \'s/[\\\\"]/\\\\&/g\' "{MAIL_ROOT}/owner.txt")\n'
            f'  name=$(sed \'s/[\\\\"]/\\\\&/g\' "{MAIL_ROOT}/owner_name.txt")\n'
            '  printf \'user_pref("mail.identity.id1.useremail", "%s");\\n\' "$address" >> "$profile/user.js"\n'
            '  printf \'user_pref("mail.identity.id1.fullName", "%s");\\n\' "$name" >> "$profile/user.js"\n'
            "fi\n"
            'exec /opt/thunderbird/thunderbird -profile "$profile" "$@"\n')


def mail_client_files(user: str) -> dict[str, str]:
    return {DOVECOT_CONFIG.as_posix().lstrip("/"): DOVECOT_IMAP_CONFIG,
            PROFILE_TEMPLATE.lstrip("/"): profile_template(user),
            THUNDERBIRD_LAUNCHER.lstrip("/"): thunderbird_launcher(),
            "etc/thunderbird/policies/policies.json": THUNDERBIRD_POLICIES}
