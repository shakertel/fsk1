import os, json, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from supabase import create_client
from google import genai

MODEL="gemini-3.5-flash-lite"
# v4: shared key pool architecture planned
BATCH_SIZE=150
MAX_RESULTS=100
FINAL_CANDIDATE_LIMIT=100

class SearchCancelled(Exception): pass

class FSKEngine:
    def __init__(self):
        url=os.environ["SUPABASE_URL"]
        key=os.environ["SUPABASE_SECRET_KEY"]
        self.sb=create_client(url,key)

        # Keep the original GEMINI_KEY numbers for clear logs.
        all_keys=[]
        for i in range(1,15):
            k=os.getenv(f"GEMINI_KEY_{i}")
            if k:
                all_keys.append((i,k))
        if not all_keys:
            raise RuntimeError("No GEMINI_KEY_* configured")

        # CHANGE 1: two independent API-key groups.
        # With 14 configured keys this is 1..7 and 8..14.
        split=(len(all_keys)+1)//2
        self.key_pools=[all_keys[:split], all_keys[split:]]
        if not self.key_pools[1]:
            # Safety fallback if only one key exists.
            self.key_pools[1]=self.key_pools[0]

        self._pool_indices=[0,0]
        self._pool_locks=[threading.Lock(),threading.Lock()]

        self.series=[]
        self._load_data()

        print(f"FSK ENGINE READY | {len(self.series)} titles loaded", flush=True)

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
        self.series=self._load_all(
            "series","id,title,description,recommendation_tags,imdb_rating,year,countries,genres,poster_url,seasons_count"
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

    def gemini_call(self,prompt,cancel_event=None,pool_id=0):
        self._check(cancel_event)
        pool=self.key_pools[pool_id]
        lock=self._pool_locks[pool_id]
        last=None

        print(f"=== GEMINI CALL START | POOL {pool_id+1} ===",flush=True)

        for attempt in range(len(pool)):
            self._check(cancel_event)

            with lock:
                slot=self._pool_indices[pool_id] % len(pool)

            key_number,api_key=pool[slot]
            print(
                f"POOL {pool_id+1} TRY KEY {key_number} ATTEMPT {attempt+1}",
                flush=True
            )

            try:
                start=time.time()
                client=genai.Client(api_key=api_key)
                response=client.models.generate_content(
                    model=MODEL,
                    contents=prompt
                )
                text=response.text
                elapsed=round(time.time()-start,2)

                print(
                    f"POOL {pool_id+1} KEY {key_number} DONE {elapsed} sec",
                    flush=True
                )

                if not text:
                    raise RuntimeError("Empty Gemini response")
                return text

            except Exception as e:
                last=e
                print(
                    f"POOL {pool_id+1} KEY {key_number} FAILED {repr(e)[:250]}",
                    flush=True
                )
                with lock:
                    self._pool_indices[pool_id]=(slot+1)%len(pool)

        print(
            f"POOL {pool_id+1} ALL KEYS FAILED {repr(last)}",
            flush=True
        )
        raise RuntimeError(f"Gemini pool {pool_id+1} unavailable: {last}")

    def _run_batch(self,i,batch,user_query,intent,cancel_event,pool_id):
        self._check(cancel_event)
        print(
            f"=== BATCH START === {i} | POOL {pool_id+1}",
            flush=True
        )

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
{json.dumps([
    {
        "id": s.get("id"),
        "title": s.get("title"),
        "description": s.get("description"),
        "recommendation_tags": s.get("recommendation_tags"),
        "imdb_rating": s.get("imdb_rating"),
        "concepts": s.get("concepts"),
    }
    for s in batch
],ensure_ascii=False)}
"""
        got=self.clean_json(
            self.gemini_call(prompt,cancel_event,pool_id=pool_id)
        )
        if not isinstance(got,list):
            got=[]

        print(
            f"=== BATCH DONE === {i} | POOL {pool_id+1} | GOT {len(got)}",
            flush=True
        )
        return i,got

    def _run_lane(self,jobs,user_query,intent,cancel_event,pool_id):
        """Each lane is sequential internally; the two lanes run in parallel."""
        lane_results=[]
        for i,batch in jobs:
            self._check(cancel_event)
            try:
                _,got=self._run_batch(
                    i,batch,user_query,intent,cancel_event,pool_id
                )
                lane_results.extend(got)
            except SearchCancelled:
                raise
            except Exception as e:
                print(
                    f"BATCH ERROR {i} | POOL {pool_id+1} | {repr(e)}",
                    flush=True
                )
        return lane_results

    def search(self,user_query,cancel_event=None):
        self._check(cancel_event)
        search_start=time.time()
        print('=== SEARCH START ===',user_query,flush=True)

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
        print('=== INTENT GEMINI START ===',flush=True)
        intent=self.clean_json(
            self.gemini_call(intent_prompt,cancel_event,pool_id=0)
        )
        print('=== INTENT DONE ===',flush=True)

        # Build all batches, then split them exactly into two halves.
        jobs=[
            (i,self.series[i:i+BATCH_SIZE])
            for i in range(0,len(self.series),BATCH_SIZE)
        ]
        mid=(len(jobs)+1)//2
        lane1=jobs[:mid]
        lane2=jobs[mid:]

        print(
            f"=== PARALLEL BATCHES === TOTAL {len(jobs)} | "
            f"POOL 1: {len(lane1)} | POOL 2: {len(lane2)}",
            flush=True
        )

        candidates=[]
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures=[
                ex.submit(
                    self._run_lane,lane1,user_query,intent,cancel_event,0
                ),
                ex.submit(
                    self._run_lane,lane2,user_query,intent,cancel_event,1
                )
            ]
            for f in as_completed(futures):
                candidates.extend(f.result())

        print(
            f"=== RAW CANDIDATES === {len(candidates)}",
            flush=True
        )

        # Deduplicate using the strongest batch score if a title somehow repeats.
        dedup={}
        for c in candidates:
            if not isinstance(c,dict):
                continue
            title=c.get("title")
            if not title or title not in self.lookup:
                continue

            def preliminary_score(x):
                try:
                    exp=max(0,min(40,float(x.get("experience_score",0) or 0)))
                except:
                    exp=0
                try:
                    pref=max(0,min(60,float(x.get("preference_score",0) or 0)))
                except:
                    pref=0
                return exp+pref

            if (
                title not in dedup or
                preliminary_score(c)>preliminary_score(dedup[title])
            ):
                dedup[title]=c

        candidates=list(dedup.values())

        # CHANGE 2: Final ranking never receives more than 100 candidates.
        candidates.sort(
            key=lambda c: (
                max(0,min(40,float(c.get("experience_score",0) or 0)))
                if str(c.get("experience_score",0)).replace(".","",1).isdigit()
                else 0
            ) + (
                max(0,min(60,float(c.get("preference_score",0) or 0)))
                if str(c.get("preference_score",0)).replace(".","",1).isdigit()
                else 0
            ),
            reverse=True
        )
        candidates=candidates[:FINAL_CANDIDATE_LIMIT]

        print(
            f"=== FINAL CANDIDATES AFTER DEDUP/LIMIT === {len(candidates)}",
            flush=True
        )

        if not candidates:
            raise RuntimeError("No candidates found")

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
        print('=== FINAL GEMINI START ===',flush=True)
        ranking=self.clean_json(
            self.gemini_call(final_prompt,cancel_event,pool_id=1)
        )
        print('=== FINAL GEMINI DONE ===',flush=True)

        if not isinstance(ranking,list):
            raise RuntimeError("Final ranking is not a list")

        out=[]
        seen=set()
        for r in ranking:
            if not isinstance(r,dict): continue
            title=r.get("title")
            if not title or title in seen or title not in self.lookup: continue
            seen.add(title)
            db=self.lookup[title]
            try:
                exp=max(0,min(40,int(float(r.get("experience_score",0) or 0))))
            except:
                exp=0
            try:
                pref=max(0,min(60,int(float(r.get("preference_score",0) or 0))))
            except:
                pref=0
            out.append({
                "title":title,
                "experience_score":exp,
                "preference_score":pref,
                "score":min(100,exp+pref),
                "star":bool(r.get("star",False)),
                "star_reason":str(r.get("star_reason","") or ""),
                "reason":str(r.get("reason","") or ""),
                "imdb_rating":db.get("imdb_rating"),
                "year":db.get("year"),
                "countries":db.get("countries") or [],
                "genres":db.get("genres") or [],
                "poster_url":db.get("poster_url"),
                "seasons_count":db.get("seasons_count"),
                "description":db.get("description") or "خلاصه داستان در دیتابیس موجود نیست."
            })

        out.sort(key=lambda x:x["score"],reverse=True)
        elapsed=round(time.time()-search_start,2)
        print(
            f"=== SEARCH FINISHED | {len(out[:MAX_RESULTS])} RESULTS | {elapsed} sec ===",
            flush=True
        )
        return out[:MAX_RESULTS]
