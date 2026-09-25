import os, json, threading
from supabase import create_client
from google import genai

MODEL="gemini-3.5-flash-lite"
BATCH_SIZE=150
MAX_RESULTS=100

class SearchCancelled(Exception): pass

class FSKEngine:
    def __init__(self):
        url=os.environ["SUPABASE_URL"]
        key=os.environ["SUPABASE_SECRET_KEY"]
        self.sb=create_client(url,key)
        self.keys=[os.getenv(f"GEMINI_KEY_{i}") for i in range(1,15)]
        self.keys=[k for k in self.keys if k]
        if not self.keys:
            raise RuntimeError("No GEMINI_KEY_* configured")
        self._key_index=0
        self._key_lock=threading.Lock()
        self.series=[]
        self._load_data()

    def _check(self,cancel_event):
        if cancel_event and cancel_event.is_set():
            raise SearchCancelled()

    def _load_all(self,table,columns):
        out=[]; offset=0
        while True:
            rows=(self.sb.table(table).select(columns)
                  .range(offset,offset+999).execute().data or [])
            if not rows: break
            out.extend(rows)
            if len(rows)<1000: break
            offset+=1000
        return out

    def _load_data(self):
        # description is deliberately loaded: it is both search evidence and final story presentation.
        self.series=self._load_all(
            "series","id,title,description,recommendation_tags,imdb_rating"
        )
        concepts=self._load_all("concepts","series_id,concept")
        cmap={}
        for c in concepts:
            cmap.setdefault(c.get("series_id"),[]).append(c.get("concept"))
        for s in self.series:
            s["concepts"]=[x for x in cmap.get(s.get("id"),[]) if x]
        self.lookup={s.get("title"):s for s in self.series if s.get("title")}

    @staticmethod
    def clean_json(text):
        text=(text or "").replace("```json","").replace("```","").strip()
        dec=json.JSONDecoder()
        starts=[p for p in (text.find("{"),text.find("[")) if p!=-1]
        if not starts: raise ValueError("Gemini returned no JSON")
        data,_=dec.raw_decode(text[min(starts):])
        return data

    def gemini_call(self,prompt,cancel_event=None):
        self._check(cancel_event)
        last=None
        # One pass through all 14 configured keys. The next call begins from the last good/current key.
        for _ in range(len(self.keys)):
            self._check(cancel_event)
            with self._key_lock:
                idx=self._key_index % len(self.keys)
            try:
                client=genai.Client(api_key=self.keys[idx])
                response=client.models.generate_content(model=MODEL,contents=prompt)
                text=response.text
                if not text: raise RuntimeError("Empty Gemini response")
                return text
            except Exception as e:
                last=e
                with self._key_lock:
                    self._key_index=(idx+1)%len(self.keys)
        raise RuntimeError(f"Gemini unavailable: {last}")

    def search(self,user_query,cancel_event=None):
        self._check(cancel_event)
        intent_prompt=f"""
تو یک سیستم تحلیل نیاز کاربر برای پیشنهاد فیلم و سریال هستی.

جمله کاربر:
{user_query}

وظیفه تو این است که بفهمی کاربر از چه «تجربه انسانی» صحبت می‌کند، نه اینکه فقط کلمات را به ژانر تبدیل کنی.

خیلی مهم:
اگر کاربر می‌گوید «من مریضم»، معنی آن این نیست که سریال بیمارستانی، پزشکی یا داستان دکترها می‌خواهد.
بلکه ممکن است منظور تجربه انسانیِ روبه‌رو شدن با بیماری، کنار آمدن با بیماری، امید در شرایط سخت،
ادامه دادن زندگی با وجود مشکل جسمی، درمان و بهبود یا پذیرش شرایط باشد.

پس:
بیماری کاربر ≠ ژانر پزشکی
فوت عزیز یا فوت پدر کاربر ≠ سریال جنایی

مثال:
برای «من مریضم و حالم خوب نیست»، The Big C می‌تواند تطابق مستقیم تجربه انسانی باشد چون شخصیت اصلی
با بیماری زندگی می‌کند. اما Hospital Playlist، House M.D. و The Good Doctor صرفاً به دلیل پزشک یا
بیمارستان بودن، تطابق مستقیم تجربه کاربر محسوب نمی‌شوند.

اول استخراج کن:
1) تجربه واقعی کاربر، اگر وجود داشت:
type می‌تواند illness, grief, breakup, loneliness, financial_problem, mental_crisis, restart یا مورد مناسب دیگری باشد.
description: توضیح انسانی تجربه.
story_requirements: چیزهایی که برای تطابق مستقیم تجربه باید در داستان پیدا شوند.
story_exclusions: چیزهایی که نباید اشتباه گرفته شوند.

2) ترجیحات عمومی، فقط اگر در پیام کاربر شاهد دارند؛ مثل حال خوب کن، امیدبخش، طنز، شخصیت دوست‌داشتنی،
کشش داستانی، آرامش یا خانوادگی. از خودت ترجیح نساز.

خروجی فقط JSON:
{{
 "experience":{{
   "detected":false,
   "type":"",
   "description":"",
   "story_requirements":[],
   "story_exclusions":[]
 }},
 "preferences":[]
}}
"""
        intent=self.clean_json(self.gemini_call(intent_prompt,cancel_event))
        candidates=[]
        for i in range(0,len(self.series),BATCH_SIZE):
            self._check(cancel_event)
            batch=self.series[i:i+BATCH_SIZE]
            prompt=f"""
کاربر:
{user_query}

Intent:
{json.dumps(intent,ensure_ascii=False)}

از بین داده‌ها حداکثر 10 سریال مناسب انتخاب کن.

قانون بسیار مهم ⭐:
star فقط وقتی true است که تجربه واقعی کاربر به شکل مستقیم در داستان، توضیحات، concept یا tag اثر وجود داشته باشد.
مثال بیماری:
The Big C = true
Friends = false
Ted Lasso = false
صرفاً حال‌خوب‌کن بودن، پزشکی بودن یا بیمارستانی بودن دلیل star نیست.

امتیاز:
experience_score حداکثر 40
preference_score حداکثر 60
IMDb در امتیاز دخالت نکند.
فقط عنوان واقعی موجود در DATA را خروجی بده.

JSON فقط:
[
{{
"title":"",
"experience_score":0,
"preference_score":0,
"star":false,
"star_reason":"",
"reason":""
}}
]

DATA:
{json.dumps(batch,ensure_ascii=False)}
"""
            try:
                got=self.clean_json(self.gemini_call(prompt,cancel_event))
                if isinstance(got,list): candidates.extend(got)
            except SearchCancelled:
                raise
            except Exception as e:
                print("BATCH ERROR",i,repr(e),flush=True)

        dedup={}
        for c in candidates:
            t=c.get("title") if isinstance(c,dict) else None
            if t and t in self.lookup: dedup[t]=c
        candidates=list(dedup.values())
        if not candidates: raise RuntimeError("No candidates found")

        self._check(cancel_event)
        final_prompt=f"""
رتبه بندی نهایی انجام بده.

کاربر:
{user_query}

کاندیدها:
{json.dumps(candidates,ensure_ascii=False)}

قوانین:
- score نساز.
- فقط experience_score و preference_score نگه دار.
- IMDb دخالت نکند.
- star فقط برای تجربه مستقیم داستانی با حال و روز و تجربه واقعی کاربر باشد؛ وگرنه اصلاً ستاره نگذار.
- صرفاً حال‌خوب‌کن بودن یا مناسب بودن برای شرایط کاربر، تطابق مستقیم داستانی نیست.
- فقط عنوان‌های موجود در کاندیدها را برگردان.
- همه کاندیدهای مناسب را مرتب کن، نه فقط 10 مورد؛ حداکثر 100 مورد.

JSON فقط:
[
{{
"title":"",
"experience_score":0,
"preference_score":0,
"star":false,
"star_reason":"",
"reason":""
}}
]
"""
        ranking=self.clean_json(self.gemini_call(final_prompt,cancel_event))
        if not isinstance(ranking,list): raise RuntimeError("Final ranking is not a list")

        out=[]
        seen=set()
        for r in ranking:
            if not isinstance(r,dict): continue
            title=r.get("title")
            if not title or title in seen or title not in self.lookup: continue
            seen.add(title)
            db=self.lookup[title]
            try: exp=max(0,min(40,int(float(r.get("experience_score",0) or 0))))
            except: exp=0
            try: pref=max(0,min(60,int(float(r.get("preference_score",0) or 0))))
            except: pref=0
            out.append({
                "title":title,
                "experience_score":exp,
                "preference_score":pref,
                "score":min(100,exp+pref),
                "star":bool(r.get("star",False)),
                "star_reason":str(r.get("star_reason","") or ""),
                "reason":str(r.get("reason","") or ""),
                "imdb_rating":db.get("imdb_rating"),
                "description":db.get("description") or "خلاصه داستان در دیتابیس موجود نیست."
            })
        out.sort(key=lambda x:x["score"],reverse=True)
        return out[:MAX_RESULTS]
