import os

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
)


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
# PRIVACY POLICY
# =========================================================

@router.get(
    "/privacy",
    response_class=HTMLResponse,
)
async def privacy_policy():

    return """
<!DOCTYPE html>
<html lang="ru">

<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>
        Политика конфиденциальности — Texnikach
    </title>

    <style>

        body {
            margin: 0;
            background: #111;
            color: #eee;
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                Arial,
                sans-serif;
            line-height: 1.65;
        }

        .container {
            max-width: 850px;
            margin: 0 auto;
            padding: 40px 22px 70px;
        }

        h1 {
            font-size: 34px;
            margin-bottom: 10px;
        }

        h2 {
            margin-top: 34px;
            font-size: 22px;
        }

        p,
        li {
            color: #ccc;
        }

        a {
            color: #d9b565;
        }

        .updated {
            color: #888;
            margin-bottom: 35px;
        }

    </style>

</head>

<body>

<div class="container">

    <h1>
        Политика конфиденциальности Texnikach
    </h1>

    <div class="updated">
        Последнее обновление: 17 августа 2026 г.
    </div>

    <p>
        Настоящая Политика конфиденциальности описывает,
        как Texnikach обрабатывает данные при использовании
        автоматизированных функций, связанных с Instagram.
    </p>

    <h2>
        1. Какие данные могут обрабатываться
    </h2>

    <p>
        При взаимодействии пользователя с Instagram-аккаунтом
        Texnikach приложение может получать через официальные
        инструменты Meta и Instagram API данные, необходимые
        для работы автоматизации.
    </p>

    <ul>
        <li>
            идентификатор пользователя Instagram;
        </li>

        <li>
            имя пользователя Instagram;
        </li>

        <li>
            текст комментариев и сообщений;
        </li>

        <li>
            идентификаторы публикаций, Reels и комментариев;
        </li>

        <li>
            технические данные, предоставляемые Instagram API.
        </li>
    </ul>

    <h2>
        2. Для чего используются данные
    </h2>

    <p>
        Данные используются исключительно для работы сервиса
        Texnikach, включая:
    </p>

    <ul>
        <li>
            обработку запросов клиентов;
        </li>

        <li>
            ответы на комментарии;
        </li>

        <li>
            отправку информации о товарах в Instagram Direct;
        </li>

        <li>
            передачу запроса живому менеджеру при необходимости;
        </li>

        <li>
            обеспечение стабильной и безопасной работы сервиса.
        </li>
    </ul>

    <h2>
        3. Данные о товарах
    </h2>

    <p>
        Информация о ценах, наличии и характеристиках товаров
        формируется на основе внутренних данных Texnikach.
        Пользовательские данные не используются для определения
        или изменения стоимости товаров.
    </p>

    <h2>
        4. Передача данных третьим лицам
    </h2>

    <p>
        Texnikach не продаёт персональные данные пользователей.
        Данные могут обрабатываться сервисами Meta и Instagram
        в соответствии с их собственными правилами и условиями.
    </p>

    <h2>
        5. Хранение данных
    </h2>

    <p>
        Мы стараемся хранить только те данные, которые необходимы
        для работы сервиса, технической диагностики и обслуживания
        клиентов. Данные удаляются, когда необходимость в их
        обработке прекращается, если иное не требуется законом.
    </p>

    <h2>
        6. Безопасность
    </h2>

    <p>
        Для защиты данных используются технические и
        организационные меры. Ключи доступа и секретные данные
        приложения не размещаются в открытом доступе.
    </p>

    <h2>
        7. Удаление данных
    </h2>

    <p>
        Пользователь может запросить удаление связанных с ним
        данных, отправив запрос на электронную почту:
    </p>

    <p>
        <a href="mailto:texnikach@gmail.com">
            texnikach@gmail.com
        </a>
    </p>

    <p>
        В запросе необходимо указать Instagram-аккаунт,
        данные которого требуется удалить.
    </p>

    <h2>
        8. Контакты
    </h2>

    <p>
        Texnikach<br>
        Ташкент, Узбекистан
    </p>

    <p>
        Email:
        <a href="mailto:texnikach@gmail.com">
            texnikach@gmail.com
        </a>
    </p>

    <p>
        Сайт:
        <a
            href="https://texnikach.uz"
            target="_blank"
            rel="noopener noreferrer"
        >
            texnikach.uz
        </a>
    </p>

</div>

</body>

</html>
    """


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