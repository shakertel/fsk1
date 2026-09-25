
import os, json, threading, concurrent.futures, time
from supabase import create_client
from google import genai

MODEL = "gemini-3.5-flash-lite"
BATCH_SIZE = 150
MAX_RESULTS = 100
GEMINI_TIMEOUT = 10

class SearchCancelled(Exception):
    pass

class FSKEngine:
    def __init__(self):
        self.sb = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SECRET_KEY"]
        )

        self.keys = [
            os.getenv(f"GEMINI_KEY_{i}")
            for i in range(1,15)
        ]
        self.keys = [k for k in self.keys if k]

        if not self.keys:
            raise RuntimeError("No Gemini keys")

        self.key_groups = [
            [0,3,7,11],
            [1,4,8],
            [2,5,9],
            [6,10,12,13],
        ]

        self.series = []
        self._load_data()

    def _load_all(self, table, columns):
        out=[]
        offset=0
        while True:
            rows=(self.sb.table(table)
                  .select(columns)
                  .range(offset, offset+999)
                  .execute()
                  .data or [])
            if not rows:
                break
            out.extend(rows)
            if len(rows)<1000:
                break
            offset+=1000
        return out

    def _load_data(self):
        print("ENGINE LOAD START", flush=True)

        self.series=self._load_all(
            "series",
            "id,title,description,recommendation_tags,imdb_rating"
        )

        concepts=self._load_all(
            "concepts",
            "series_id,concept"
        )

        cmap={}
        for c in concepts:
            cmap.setdefault(c["series_id"],[]).append(c["concept"])

        for s in self.series:
            s["concepts"]=cmap.get(s["id"],[])

        self.lookup={x["title"]:x for x in self.series}

        print(
            "ENGINE READY",
            len(self.series),
            flush=True
        )

    @staticmethod
    def clean_json(text):
        text=(text or "").replace("```json","").replace("```","").strip()
        dec=json.JSONDecoder()
        pos=min(
            [x for x in [
                text.find("["),
                text.find("{")
            ] if x!=-1]
        )
        return dec.raw_decode(text[pos:])[0]

    def gemini_call(self,prompt,key_indexes):
        last=None

        for idx in key_indexes:
            try:
                key=self.keys[idx]

                print(
                    "GEMINI TRY KEY",
                    idx+1,
                    flush=True
                )

                result=[None]

                def run():
                    try:
                        result[0]=genai.Client(
                            api_key=key
                        ).models.generate_content(
                            model=MODEL,
                            contents=prompt
                        ).text
                    except Exception as e:
                        result.append(e)

                t=threading.Thread(target=run)
                t.start()
                t.join(GEMINI_TIMEOUT)

                if t.is_alive():
                    print(
                        "TIMEOUT KEY",
                        idx+1,
                        flush=True
                    )
                    continue

                if result[0]:
                    return result[0]

            except Exception as e:
                last=e

        raise RuntimeError(
            f"Gemini failed {last}"
        )

    def process_worker(self, worker_id, data, key_group):
        print(
            "WORKER START",
            worker_id,
            len(data),
            flush=True
        )

        out=[]

        for i in range(0,len(data),BATCH_SIZE):
            batch=data[i:i+BATCH_SIZE]

            prompt=f"""
کاربر:
{self.user_query}

از بین داده ها حداکثر 10 سریال انتخاب کن.

DATA:
{json.dumps(batch,ensure_ascii=False)}

JSON:
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

            try:
                x=self.clean_json(
                    self.gemini_call(
                        prompt,
                        key_group
                    )
                )
                if isinstance(x,list):
                    out.extend(x)

                print(
                    "WORKER",
                    worker_id,
                    "DONE",
                    i,
                    flush=True
                )

            except Exception as e:
                print(
                    "WORKER ERROR",
                    worker_id,
                    repr(e),
                    flush=True
                )

        return out

    def search(self,user_query,cancel_event=None):
        self.user_query=user_query

        print(
            "SEARCH START",
            user_query,
            flush=True
        )

        chunks=[]
        size=len(self.series)//4

        for i in range(4):
            chunks.append(
                self.series[
                    i*size:
                    (i+1)*size if i<3 else len(self.series)
                ]
            )

        candidates=[]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures=[]

            for i,ch in enumerate(chunks):
                futures.append(
                    ex.submit(
                        self.process_worker,
                        i+1,
                        ch,
                        self.key_groups[i]
                    )
                )

            for f in futures:
                candidates.extend(f.result())

        print(
            "ALL WORKERS DONE",
            len(candidates),
            flush=True
        )

        # ادامه رتبه بندی نهایی مثل نسخه قبلی اینجا قرار می گیرد
        return candidates[:MAX_RESULTS]
