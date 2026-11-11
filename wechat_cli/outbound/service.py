from __future__ import annotations

import time

from .models import (
    OutboundActionError,
    OutboundError,
    SendResult,
    SendStatus,
)

MAX_TEXT_CODEPOINTS = 4096


class OutboundService:
    def __init__(
        self,
        adapter,
        verifier,
        *,
        clock=time.time,
        verification_timeout_seconds: float = 8.0,
        verification_poll_seconds: float = 0.5,
    ):
        self.adapter = adapter
        self.verifier = verifier
        self.clock = clock
        self.verification_timeout_seconds = float(verification_timeout_seconds)
        self.verification_poll_seconds = float(verification_poll_seconds)

    def send(self, target, text: str) -> SendResult:
        body = str(text or "")
        if not body.strip() or len(body) > MAX_TEXT_CODEPOINTS:
            return SendResult(
                status=SendStatus.BLOCKED,
                target=target,
                code=OutboundError.INVALID_TEXT,
                message=f"Text must contain 1-{MAX_TEXT_CODEPOINTS} Unicode code points.",
            )

        probe = self.adapter.probe()
        if not probe.ok:
            return SendResult(
                status=SendStatus.BLOCKED,
                target=target,
                code=probe.code or OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message=probe.message,
            )

        try:
            self.adapter.open_target(target)
        except OutboundActionError as exc:
            return SendResult(
                status=SendStatus.BLOCKED,
                target=target,
                code=exc.code,
                message=str(exc),
            )
        except Exception as exc:
            return SendResult(
                status=SendStatus.BLOCKED,
                target=target,
                code=OutboundError.TARGET_UI_MISMATCH,
                message=f"Target could not be verified in the WeChat UI: {exc}",
            )

        attempted_at = int(self.clock())
        try:
            self.adapter.send_text_once(body)
        except Exception as exc:
            return SendResult(
                status=SendStatus.FAILED,
                target=target,
                code=OutboundError.SEND_ACTION_FAILED,
                message=f"Send action failed: {exc}",
                attempted_at=attempted_at,
            )

        try:
            matched = self.verifier.wait_for_match(
                target,
                body,
                not_before=max(0, attempted_at - 2),
                timeout_seconds=self.verification_timeout_seconds,
                poll_seconds=self.verification_poll_seconds,
            )
        except Exception as exc:
            return SendResult(
                status=SendStatus.UNCONFIRMED,
                target=target,
                code=OutboundError.SEND_UNCONFIRMED,
                message=(
                    "The UI send action ran, but readback verification failed; "
                    f"the message may already have been sent and must not be auto-retried. ({exc})"
                ),
                attempted_at=attempted_at,
            )

        if matched is None:
            return SendResult(
                status=SendStatus.UNCONFIRMED,
                target=target,
                code=OutboundError.SEND_UNCONFIRMED,
                message=(
                    "The UI send action ran, but no matching self-authored message was observed; "
                    "the message may already have been sent and must not be auto-retried."
                ),
                attempted_at=attempted_at,
            )

        try:
            confirmed_at = int(matched.get("timestamp"))
        except (AttributeError, TypeError, ValueError):
            confirmed_at = int(self.clock())
        return SendResult(
            status=SendStatus.CONFIRMED,
            target=target,
            code=None,
            message="Message send confirmed by the local read path.",
            attempted_at=attempted_at,
            confirmed_at=confirmed_at,
        )
