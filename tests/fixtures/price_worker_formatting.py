"""Pure production formatter fixture from catalog-safety-20261007.

No configuration, IO, credentials or publication code. Used only to verify
that the server sorting overlay changes order while preserving formatting.
"""
import html

som = 0

def render_inline_text(value):

    parts = str(value).split("**")

    if len(parts) % 2 == 0:

        return html.escape(
            str(value),
            quote=False,
        )

    rendered_parts = []

    for index, part in enumerate(parts):

        escaped_part = html.escape(
            part,
            quote=False,
        )

        if index % 2:

            rendered_parts.append(
                f"<strong>{escaped_part}</strong>"
            )

        else:

            rendered_parts.append(
                escaped_part
            )

    return "".join(rendered_parts)

def split_text(
    text,
    width=370,
):

    words = (
        str(text)
        .split()
    )

    line_length = 0

    lines = []

    current_line = []

    for word in words:

        additional = (
            len(word)
            +
            (
                1
                if current_line
                else 0
            )
        )

        if (
            line_length
            + additional
            > width
        ):

            lines.append(
                " ".join(
                    current_line
                )
            )

            current_line = []

            line_length = 0

        current_line.append(
            word
        )

        line_length += (
            len(word)
            +
            (
                1
                if line_length > 0
                else 0
            )
        )

    if current_line:

        lines.append(
            " ".join(
                current_line
            )
        )

    return (
        "<br>&nbsp;&nbsp;"
        .join(
            render_inline_text(line)
            for line in lines
        )
    )

def summarize_statuses(
    statuses
):

    statuses = (
        statuses
        .dropna()
        .astype(str)
    )

    filtered_statuses = (
        statuses[
            statuses
            != "None"
        ]
    )

    if filtered_statuses.empty:

        return ""

    status_counts = (
        filtered_statuses
        .value_counts()
    )

    if (
        "Up"
        in status_counts

        and

        "Down"
        in status_counts
    ):

        if (
            status_counts[
                "Up"
            ]
            >
            status_counts[
                "Down"
            ]
        ):

            return "🔺"

        return "🔻"

    if (
        "Up"
        in status_counts
    ):

        return "🔺"

    if (
        "Down"
        in status_counts
    ):

        return "🔻"

    return ""

def clean_price(
    price
):

    try:

        value = float(
            price
        )

        if (
            value.is_integer()
        ):

            return str(
                int(value)
            )

        return (
            f"{value:.2f}"
            .rstrip("0")
            .rstrip(".")
        )

    except Exception:

        return str(
            price
        )

def format_prices(
    group
):

    if group.empty:

        return ""

    has_memory = (
        "memory"
        in group.columns

        and

        group[
            "memory"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    )

    has_color = (
        "color"
        in group.columns

        and

        group[
            "color"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    )

    details = []

    model_name = (
        group[
            "model_with_link"
        ]
        .iloc[0]
    )

    # ========================================================
    # MEMORY + COLOR
    # ========================================================

    if (
        has_memory
        and
        has_color
    ):

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "memory",
                    "price",
                ],
                sort=False,
                dropna=False,
            )
        )

        for (
            memory,
            price,
        ), sub_group_df in sub_group:

            status = (
                summarize_statuses(
                    sub_group_df[
                        "status"
                    ]
                )
            )

            status_text = (
                f" {status}"
                if status
                else ""
            )

            colors = (
                sub_group_df[
                    "color"
                ]
                .fillna("")
                .astype(str)
                .str.strip()
                .tolist()
            )

            colors = [
                color

                for color
                in dict.fromkeys(
                    colors
                )

                if color
            ]

            colors_text = (
                ", ".join(
                    colors
                )
            )

            if som == 1:

                price_formatted = (
                    f"{float(price):,.0f}"
                    .replace(
                        ",",
                        " "
                    )
                )

                detail_line = (
                    f"• {memory} "
                    f"{colors_text}: "
                    f"**{price_formatted} Сум**"
                    f"{status_text}"
                )

            else:

                detail_line = (
                    f"• {memory} "
                    f"{colors_text}: "
                    f"**{clean_price(price)}**"
                    f"{status_text}"
                )

            details.append(
                split_text(
                    detail_line
                )
            )

    # ========================================================
    # MEMORY ONLY
    # ========================================================

    elif has_memory:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "memory",
                    "price",
                ],
                sort=False,
                dropna=False,
            )
        )

        for (
            memory,
            price,
        ), sub_group_df in sub_group:

            status = (
                summarize_statuses(
                    sub_group_df[
                        "status"
                    ]
                )
            )

            status_text = (
                f" {status}"
                if status
                else ""
            )

            if som == 1:

                price_formatted = (
                    f"{float(price):,.0f}"
                    .replace(
                        ",",
                        " "
                    )
                )

                detail_line = (
                    f"• {memory}: "
                    f"**{price_formatted} Сум**"
                    f"{status_text}"
                )

            else:

                detail_line = (
                    f"• {memory}: "
                    f"**{clean_price(price)}**"
                    f"{status_text}"
                )

            details.append(
                split_text(
                    detail_line
                )
            )

    # ========================================================
    # COLOR ONLY
    # ========================================================

    elif has_color:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                "price",
                sort=False,
                dropna=False,
            )
        )

        for (
            price,
            sub_group_df,
        ) in sub_group:

            status = (
                summarize_statuses(
                    sub_group_df[
                        "status"
                    ]
                )
            )

            status_text = (
                f" {status}"
                if status
                else ""
            )

            colors = (
                sub_group_df[
                    "color"
                ]
                .fillna("")
                .astype(str)
                .str.strip()
                .tolist()
            )

            colors = [
                color

                for color
                in dict.fromkeys(
                    colors
                )

                if color
            ]

            colors_text = (
                ", ".join(
                    colors
                )
            )

            if som == 1:

                price_formatted = (
                    f"{float(price):,.0f}"
                    .replace(
                        ",",
                        " "
                    )
                )

                detail_line = (
                    f"• {colors_text}: "
                    f"**{price_formatted} Сум**"
                    f"{status_text}"
                )

            else:

                detail_line = (
                    f"• {colors_text}: "
                    f"**{clean_price(price)}**"
                    f"{status_text}"
                )

            details.append(
                split_text(
                    detail_line
                )
            )

    # ========================================================
    # PRICE ONLY
    # ========================================================

    else:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                "price",
                sort=False,
                dropna=False,
            )
        )

        for (
            price,
            sub_group_df,
        ) in sub_group:

            status = (
                summarize_statuses(
                    sub_group_df[
                        "status"
                    ]
                )
            )

            status_text = (
                f" {status}"
                if status
                else ""
            )

            if som == 1:

                price_formatted = (
                    f"{float(price):,.0f}"
                    .replace(
                        ",",
                        " "
                    )
                )

                detail_line = (
                    f"{price_formatted} Сум"
                    f"{status_text}"
                )

            else:

                detail_line = (
                    f"{clean_price(price)}"
                    f"{status_text}"
                )

            details.append(
                split_text(
                    detail_line
                )
            )

    # ========================================================
    # RETURN
    # ========================================================

    if (
        details
        and
        (
            has_memory
            or
            has_color
        )
    ):

        return (
            f"{model_name}"
            "<br>"
            +
            "<br>".join(
                details
            )
            +
            "<br><br>"
        )

    return (
        f"{model_name}: "
        +
        " ".join(
            details
        )
        +
        "<br>"
    )
