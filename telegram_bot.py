
# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    print(
        "TELEGRAM ERROR",
        repr(
            context.error
        ),
        flush=True
    )


# ============================================================
# RUN BOT
# ============================================================

def run_bot():

    global engine


    token = os.getenv(
        "TELEGRAM_TOKEN"
    )


    if not token:

        raise RuntimeError(
            "TELEGRAM_TOKEN missing"
        )


    print(
        "Loading FSK database...",
        flush=True
    )


    engine = FSKEngine()


    print(
        f"FSK READY | "
        f"{len(engine.series)} titles loaded | "
        f"timeout=15m",
        flush=True
    )


    app = (
        Application
        .builder()
        .token(token)
        .build()
    )


    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        CommandHandler(
            "cancel",
            cancel
        )
    )


    app.add_handler(
        CallbackQueryHandler(
            buttons
        )
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )


    app.add_error_handler(
        error_handler
    )


    print(
        "FSK bot started",
        flush=True
    )


    app.run_polling(
        drop_pending_updates=True
    )
