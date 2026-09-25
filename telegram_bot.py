import os, asyncio, threading, time, html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from engine import FSKEngine, SearchCancelled

SEARCH_TIMEOUT=15*60
PAGE_SIZE=3
WELCOME=(
"سلام به ربات فیلم  سرچر کردستان خوش آمدید! 🎬\n\n"
"حس و حال و فیلمی که دنبالش هستید را به این سبک وارد کنید:\n\n"
"«الان حالم خوب نیست شکست عشقی خوردم دنبال یه فیلمم که بتونم سرگرم بشم و حالم خوب شه.»\n\n"
"حالا درخواست خودتان را بفرستید."
)

engine=None
states={}
jobs={}
locks={}

def keyboard(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text,callback_data=data) for text,data in row] for row in rows])

def touch(uid):
    states.setdefault(uid,{})["last_activity"]=time.monotonic()

def clear_user(uid):
    ev=jobs.pop(uid,None)
    if ev: ev.set()
    states.pop(uid,None)

def esc(x):
    return html.escape(str(x or ""))

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    clear_user(uid)
    states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
    await update.effective_message.reply_text(WELCOME,reply_markup=keyboard([("لغو ❌","cancel")]))

async def cancel(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    clear_user(uid)
    states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
    msg="جستجو لغو شد.\n\n"+WELCOME
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg,reply_markup=keyboard([("لغو ❌","cancel")]))
    else:
        await update.effective_message.reply_text(msg,reply_markup=keyboard([("لغو ❌","cancel")]))

async def text_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    st=states.get(uid)
    if st and st.get("stage")=="searching":
        await update.message.reply_text("جستجوی قبلی هنوز در حال انجام است. برای توقف، «لغو» را بزنید.",
                                        reply_markup=keyboard([("لغو جستجو ❌","cancel")]))
        return
    q=(update.message.text or "").strip()
    if not q: return
    states[uid]={"stage":"confirm","query":q,"last_activity":time.monotonic()}
    await update.message.reply_text(
        "درخواست شما:\n\n"+q+"\n\nآیا این پیام نهایی شماست؟",
        reply_markup=keyboard([("تأیید و جستجو بژی کوردیستان✅","confirm")],[("لغو ❌","cancel")])
    )

async def show_page(chat_id,uid,context):
    st=states.get(uid)
    if not st or st.get("stage")!="results": return
    results=st["results"]; pos=st.get("pos",0)
    end=min(pos+PAGE_SIZE,len(results))
    for idx in range(pos,end):
        x=results[idx]
        star="⭐ " if x.get("star") else ""
        imdb=x.get("imdb_rating")
        imdb="-" if imdb is None else imdb
        text=(
            f"{star}<b>{idx+1} - {esc(x.get('title'))}</b>\n"
            f"🎬 IMDb: {esc(imdb)}\n"
            f"🎯 Score: {esc(x.get('score'))}\n\n"
            f"📖 <b>خلاصه داستان:</b>\n{esc(x.get('description'))}\n\n"
            f"🎯 <b>دلیل پیشنهاد:</b>\n{esc(x.get('reason'))}"
        )
        if x.get("star") and x.get("star_reason"):
            text+=f"\n\n⭐ <b>تطابق مستقیم با تجربه شما:</b>\n{esc(x.get('star_reason'))}"
        await context.bot.send_message(chat_id=chat_id,text=text,parse_mode="HTML")
    st["pos"]=end; touch(uid)
    if end<len(results):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{end} مورد از {len(results)} پیشنهاد نمایش داده شد. ادامه بدهم؟",
            reply_markup=keyboard([("۳ تای بعدی ▶️","next")],[("کافیه ✅","enough"),("لغو ❌","cancel")])
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,text="تمام پیشنهادهای این جستجو نمایش داده شد.",
            reply_markup=keyboard([("جستجوی جدید 🔎","new"),("کافیه ✅","enough")])
        )

async def run_search(query,ev):
    return await asyncio.to_thread(engine.search,query,ev)

async def confirm_search(query_obj,context):
    uid=query_obj.from_user.id
    chat_id=query_obj.message.chat_id
    if uid in jobs:
        await query_obj.answer("یک جستجو در حال اجراست.")
        return
    await query_obj.answer()
    ev=threading.Event(); jobs[uid]=ev
    states[uid]["stage"]="searching"; touch(uid)
    await query_obj.message.reply_text("🔎 جستجو شروع شد. ممکن است چند دقیقه طول بکشد.",
                                       reply_markup=keyboard([("لغو جستجو ❌","cancel")]))
    try:
        results=await asyncio.wait_for(run_search(states[uid]["query"],ev),timeout=SEARCH_TIMEOUT)
        if ev.is_set() or uid not in states: return
        if not results:
            raise RuntimeError("نتیجه‌ای پیدا نشد")
        jobs.pop(uid,None)
        states[uid]={"stage":"results","query":states[uid]["query"],"results":results,"pos":0,
                     "last_activity":time.monotonic()}
        await query_obj.message.reply_text(f"✅ {len(results)} پیشنهاد آماده شد.")
        await show_page(chat_id,uid,context)
    except asyncio.TimeoutError:
        ev.set(); jobs.pop(uid,None)
        states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
        await context.bot.send_message(chat_id=chat_id,
            text="⏱ جستجو بعد از ۱۵ دقیقه متوقف شد تا ربات گیر نکند. درخواست جدیدتان را بفرستید.")
    except SearchCancelled:
        jobs.pop(uid,None)
    except Exception as e:
        ev.set(); jobs.pop(uid,None)
        states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
        print("SEARCH ERROR",repr(e),flush=True)
        await context.bot.send_message(chat_id=chat_id,
            text="جستجو با خطا متوقف شد و ربات دوباره آماده است. درخواستتان را دوباره بفرستید.")

async def buttons(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; uid=q.from_user.id; data=q.data
    if data=="cancel":
        await cancel(update,context); return
    if data=="enough":
        await q.answer(); clear_user(uid)
        states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
        await q.message.reply_text("باشه 🌷 هر وقت خواستید درخواست جدیدتان را بفرستید."); return
    if data=="new":
        await q.answer(); clear_user(uid)
        states[uid]={"stage":"awaiting_query","last_activity":time.monotonic()}
        await q.message.reply_text(WELCOME,reply_markup=keyboard([("لغو ❌","cancel")])); return
    st=states.get(uid)
    if not st:
        await q.answer("این جستجو دیگر فعال نیست."); return
    touch(uid)
    if data=="confirm" and st.get("stage")=="confirm":
        await confirm_search(q,context); return
    if data=="next" and st.get("stage")=="results":
        await q.answer(); await show_page(q.message.chat_id,uid,context); return
    await q.answer("این دکمه دیگر فعال نیست.")

async def error_handler(update,context):
    print("TELEGRAM ERROR",repr(context.error),flush=True)

def run_bot():
    global engine
    token=os.getenv("TELEGRAM_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_TOKEN missing")
    print("Loading FSK database...",flush=True)
    engine=FSKEngine()
    print(f"FSK READY | {len(engine.series)} titles loaded | timeout=15m",flush=True)
    app=Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("cancel",cancel))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_error_handler(error_handler)
    print("FSK bot started",flush=True)
    app.run_polling(drop_pending_updates=True)
