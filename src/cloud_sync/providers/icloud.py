"""iCloud provider — Apple-ID-password + 2FA via rclone interactive config.

Apple's iCloud Drive API uses SRP auth and rejects app-specific passwords.
The only path that works is the same flow as signing into icloud.com:
  1. POST Apple ID + real Apple password
  2. Apple pushes a 6-digit verification code to the user's trusted devices
  3. User enters the code in our UI
  4. rclone exchanges (password + code) for cookies + trust_token
  5. We persist the resulting rclone-config blob (cookies + trust_token);
     the password is NEVER stored (lives in process memory for ~30s during auth).

Trust tokens last roughly 30 days; once expired the user re-runs this flow.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ICloudCredentials:
    """What we persist for an iCloud remote.

    `apple_id` is the email/username.
    `password_obscured` is the rclone-obscured Apple password — kept so we can
       refresh trust tokens without re-prompting the user. (Trade-off: makes
       the cred blob more powerful — anyone with /data access can re-auth as
       this Apple ID + trigger a 2FA prompt. Acceptable for a single-user
       LAN-only test bench. Set `password_obscured=""` if you'd rather not
       persist; then trust-token expiry will block sync until re-add.)
    `trust_token` and `cookies` come from the rclone config flow.
    """
    apple_id: str
    password_obscured: str
    trust_token: str
    cookies: str


def serialize(c: ICloudCredentials) -> str:
    return json.dumps({
        "apple_id": c.apple_id,
        "password_obscured": c.password_obscured,
        "trust_token": c.trust_token,
        "cookies": c.cookies,
    })


def deserialize(blob: str) -> ICloudCredentials:
    d = json.loads(blob)
    return ICloudCredentials(
        apple_id=d["apple_id"],
        password_obscured=d.get("password_obscured", ""),
        trust_token=d["trust_token"],
        cookies=d["cookies"],
    )
