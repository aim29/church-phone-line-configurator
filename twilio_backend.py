"""
twilio_backend.py

All Twilio API interaction for the church phone-line service.

This is designed for a single church on a single, dedicated Twilio
account. Architecture (all hosted by Twilio — nothing runs on our own
servers):
  - A Twilio Serverless "Service" is created once.
  - The welcome message and recording(s) are uploaded as public Assets
    (static MP3 files, served from Twilio's CDN).
  - A single small Twilio Function (voice.js) is uploaded alongside them.
    It reads a JSON config (stored as an environment variable) and returns
    TwiML telling Twilio which asset(s) to play.
  - A UK number — either bought fresh or an existing one already on the
    account — has its voice webhook pointed at the deployed Function.

Two Twilio API surfaces are used:
  1. The standard REST API (wrapped by the `twilio` Python SDK) for
     buying numbers and creating Services/Assets/Functions/Builds/
     Deployments/Variables.
  2. The Serverless *upload* endpoint (serverless-upload.twilio.com),
     which is not wrapped by the SDK, for uploading the actual file
     content of an Asset or Function version. This is called directly
     with `requests`, using HTTP Basic Auth (Account SID / Auth Token) —
     the same credentials used everywhere else.

Docs:
  https://www.twilio.com/docs/serverless/api
  https://www.twilio.com/docs/serverless/api/resource/asset-version
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from twilio.rest import Client

UPLOAD_BASE = "https://serverless-upload.twilio.com/v1"
BUILD_POLL_INTERVAL_SECONDS = 2
BUILD_POLL_TIMEOUT_SECONDS = 180


class ProvisioningError(RuntimeError):
    """Raised for any failure during provisioning, with a user-facing message."""


def slugify(text: str) -> str:
    """Turn free text into a DNS-safe fragment."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "church"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


@dataclass
class TwilioBackend:
    account_sid: str
    auth_token: str
    log: callable = field(default=lambda msg: None)  # progress callback(str)

    def __post_init__(self):
        self.client = Client(self.account_sid, self.auth_token)
        self._auth = (self.account_sid, self.auth_token)

    # ------------------------------------------------------------------
    # Phone numbers
    # ------------------------------------------------------------------

    def search_uk_numbers(self, area_code: str = "", limit: int = 10) -> list[str]:
        """Search for available UK local voice-enabled numbers.

        UK numbers don't use Twilio's NANP-style `area_code` filter, so an
        area code (e.g. "0113" or "113" for Leeds) is matched with a
        `contains` pattern against the E.164 number instead.
        """
        self.log("Searching for available UK numbers...")
        kwargs = {"voice_enabled": True, "limit": limit}
        cleaned = area_code.strip().lstrip("0")
        if cleaned:
            kwargs["contains"] = f"+44{cleaned}*"

        candidates = self.client.available_phone_numbers("GB").local.list(**kwargs)
        if not candidates and cleaned:
            raise ProvisioningError(
                f"No available UK numbers found matching area code '{area_code}'. "
                "Try a different area code or leave it blank."
            )
        if not candidates:
            raise ProvisioningError("No available UK numbers were found.")
        return [c.phone_number for c in candidates]

    def buy_number(self, phone_number: str, friendly_name: str) -> tuple[str, str]:
        """Purchase a specific number. Returns (phone_number_sid, phone_number_e164)."""
        self.log(f"Purchasing {phone_number}...")
        number = self.client.incoming_phone_numbers.create(
            phone_number=phone_number, friendly_name=friendly_name
        )
        return number.sid, number.phone_number

    def list_existing_numbers(self) -> list[dict]:
        """List voice-capable numbers already on the account."""
        self.log("Loading numbers already on this account...")
        numbers = self.client.incoming_phone_numbers.list(limit=200)
        return [
            {
                "sid": n.sid,
                "phone_number": n.phone_number,
                "friendly_name": n.friendly_name,
            }
            for n in numbers
            if n.capabilities.get("voice")
        ]

    def set_number_webhook(self, phone_number_sid: str, voice_url: str) -> None:
        self.log("Pointing the number at the call handler...")
        self.client.incoming_phone_numbers(phone_number_sid).update(
            voice_url=voice_url, voice_method="POST"
        )

    # ------------------------------------------------------------------
    # Serverless service / environment
    # ------------------------------------------------------------------

    def create_service(self, friendly_name: str) -> str:
        unique_name = f"church-phone-line-{uuid.uuid4().hex[:8]}"
        self.log(f"Creating Twilio Serverless service '{unique_name}'...")
        service = self.client.serverless.v1.services.create(
            unique_name=unique_name,
            friendly_name=friendly_name,
            include_credentials=False,
            # Lets a power user open this Service in the Twilio Console
            # (Develop > Functions & Assets) and edit it directly, rather
            # than it being locked to API-only management.
            ui_editable=True,
        )
        return service.sid

    @staticmethod
    def console_url(service_sid: str) -> str:
        return f"https://console.twilio.com/us1/develop/functions/services/{service_sid}"

    def create_environment(self, service_sid: str) -> tuple[str, str]:
        """Returns (environment_sid, domain_name)."""
        self.log("Creating deployment environment...")
        env = self.client.serverless.v1.services(service_sid).environments.create(
            unique_name="production", domain_suffix="production"
        )
        return env.sid, env.domain_name

    # ------------------------------------------------------------------
    # Uploading file content (the part the SDK doesn't wrap)
    # ------------------------------------------------------------------

    def _upload_version(
        self,
        service_sid: str,
        parent_sid: str,
        kind: str,  # "Assets" or "Functions"
        path: str,
        content: bytes,
        content_type: str,
        visibility: str = "public",
    ) -> str:
        """POST the raw content of an Asset or Function version.

        This endpoint is documented but deliberately not wrapped by the
        Twilio Python SDK, so we call it directly. Per the docs
        (twilio.com/docs/serverless/api/resource/asset-version), the
        create action's accepted parameters are Content, Path, Visibility,
        ServiceSid, and AssetSid/FunctionSid — the SID fields must be
        repeated in the form body even though they're already in the URL.
        """
        url = f"{UPLOAD_BASE}/Services/{service_sid}/{kind}/{parent_sid}/Versions"
        parent_field = "AssetSid" if kind == "Assets" else "FunctionSid"
        files = {"Content": (path, content, content_type)}
        data = {
            "Path": path,
            "Visibility": visibility,
            "ServiceSid": service_sid,
            parent_field: parent_sid,
        }
        resp = requests.post(url, auth=self._auth, data=data, files=files, timeout=60)
        if resp.status_code not in (200, 201):
            raise ProvisioningError(
                f"Upload failed for {path} ({kind}): {resp.status_code} {resp.text}"
            )
        body = resp.json()
        returned_path = body.get("path")
        if returned_path != path:
            # Surfaces a mismatch immediately instead of failing silently
            # at call time, which is how this class of bug was found.
            self.log(
                f"WARNING: asked for path '{path}' but Twilio stored '{returned_path}'."
            )
        return body["sid"]

    def upload_asset(
        self, service_sid: str, friendly_name: str, path: str, local_file: Path
    ) -> str:
        """Creates a new Asset resource and uploads its content.

        Returns the Asset Version SID (needed for the next Build).
        """
        self.log(f"Uploading {local_file.name} -> {path} ...")
        asset = self.client.serverless.v1.services(service_sid).assets.create(
            friendly_name=friendly_name
        )
        content = local_file.read_bytes()
        return self._upload_version(
            service_sid, asset.sid, "Assets", path, content, "audio/mpeg"
        )

    def upload_function(
        self, service_sid: str, friendly_name: str, path: str, source_code: str
    ) -> str:
        self.log(f"Uploading call handler code -> {path} ...")
        fn = self.client.serverless.v1.services(service_sid).functions.create(
            friendly_name=friendly_name
        )
        return self._upload_version(
            service_sid,
            fn.sid,
            "Functions",
            path,
            source_code.encode("utf-8"),
            "application/javascript",
        )

    # ------------------------------------------------------------------
    # Build + deploy
    # ------------------------------------------------------------------

    def build_and_deploy(
        self,
        service_sid: str,
        environment_sid: str,
        asset_version_sids: list[str],
        function_version_sids: list[str],
    ) -> None:
        self.log("Bundling into a Build (this can take a minute)...")
        build = self.client.serverless.v1.services(service_sid).builds.create(
            asset_versions=asset_version_sids,
            function_versions=function_version_sids,
        )

        deadline = time.time() + BUILD_POLL_TIMEOUT_SECONDS
        status = build.status
        while status in ("building", "queued"):
            if time.time() > deadline:
                raise ProvisioningError("Timed out waiting for the Build to complete.")
            time.sleep(BUILD_POLL_INTERVAL_SECONDS)
            build = self.client.serverless.v1.services(service_sid).builds(
                build.sid
            ).fetch()
            status = build.status
            self.log(f"Build status: {status}")

        if status != "completed":
            raise ProvisioningError(f"Build failed with status '{status}'.")

        self.log("Deploying to the live environment...")
        self.client.serverless.v1.services(service_sid).environments(
            environment_sid
        ).deployments.create(build_sid=build.sid)

    def set_config_variable(
        self, service_sid: str, environment_sid: str, config_json: str
    ) -> None:
        self.log("Writing call-flow configuration...")
        env = self.client.serverless.v1.services(service_sid).environments(
            environment_sid
        )
        # Variables can't be updated in place via the SDK's simple create();
        # for an update flow, list + delete any existing CONFIG_JSON first.
        for var in env.variables.list():
            if var.key == "CONFIG_JSON":
                self.client.serverless.v1.services(service_sid).environments(
                    environment_sid
                ).variables(var.sid).delete()
        env.variables.create(key="CONFIG_JSON", value=config_json)
