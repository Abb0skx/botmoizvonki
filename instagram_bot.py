import os

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from fastapi.responses import PlainTextResponse


# =========================================================
# ROUTER
# =========================================================

router = APIRouter()


# =========================================================
# CONFIG
# =========================================================

INSTAGRAM_VERIFY_TOKEN = os.getenv(
    "INSTAGRAM_VERIFY_TOKEN",
    "",
)

INSTAGRAM_ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN",
    "",
)


# =========================================================
# INSTAGRAM WEBHOOK VERIFY
# =========================================================

@router.get(
    "/webhooks/instagram",
    response_class=PlainTextResponse,
)
async def instagram_webhook_verify(
    request: Request,
):

    mode = request.query_params.get(
        "hub.mode"
    )

    verify_token = request.query_params.get(
        "hub.verify_token"
    )

    challenge = request.query_params.get(
        "hub.challenge"
    )

    print(
        "INSTAGRAM WEBHOOK VERIFY:",
        {
            "mode": mode,
            "verify_token_received":
                bool(verify_token),
            "challenge_received":
                bool(challenge),
        },
    )

    if not INSTAGRAM_VERIFY_TOKEN:

        print(
            "INSTAGRAM_VERIFY_TOKEN IS NOT SET"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Instagram verify token "
                "is not configured"
            ),
        )

    if (
        mode == "subscribe"
        and
        verify_token == INSTAGRAM_VERIFY_TOKEN
        and
        challenge is not None
    ):

        print(
            "INSTAGRAM WEBHOOK VERIFIED"
        )

        return challenge

    print(
        "INSTAGRAM WEBHOOK VERIFY FAILED"
    )

    raise HTTPException(
        status_code=403,
        detail="Invalid verify token",
    )

# =========================================================
# INSTAGRAM WEBHOOK EVENTS
# =========================================================

@router.post(
    "/webhooks/instagram"
)
async def instagram_webhook_event(
    request: Request,
):

    data = await request.json()

    print(
        "INSTAGRAM WEBHOOK EVENT:",
        data,
    )

    return {
        "ok": True
    }