
!pip -q install supabase google-genai

import json, time, math
from supabase import create_client
from google.colab import userdata
from google import genai

# ================================
# USER TEST INPUT
# ================================
# هر بار مثل یک کاربر واقعی اینجا درخواست جدید بنویس

USER_QUERY = input("🎬 درخواست کاربر را بنویسید: ")

print("\\nUSER QUERY:")
print(USER_QUERY)

sb = create_client(
    userdata.get("SUPABASE_URL"),
    userdata.get("SUPABASE_SECRET_KEY")
)

print("Supabase connected")


keys=[]
for i in range(1,15):
    try:
        k=userdata.get(f"GEMINI_KEY_{i}")
        if k:
            keys.append(k)
            print("KEY",i,"OK")
    except:
        pass

models=[

    "gemini-3.5-flash-lite",

]

ki=0
mi=0

def gemini_call(prompt):
    global ki,mi

    for _ in range(len(keys)*len(models)):
        try:
            print("TRY",models[mi],"KEY",ki+1)
            client=genai.Client(api_key=keys[ki])
            return client.models.generate_content(
                model=models[mi],
                contents=prompt
            ).text
        except Exception as e:
            print("FAIL",str(e)[:120])
            ki=(ki+1)%len(keys)
            if ki==0:
                mi=(mi+1)%len(models)

    raise Exception("Gemini unavailable")


def clean_json(text):
    text=text.replace("```json","").replace("```","").strip()

    decoder=json.JSONDecoder()

    starts=[x for x in [text.find("{"),text.find("[")] if x!=-1]

    if not starts:
        print(text)
        raise Exception("No JSON")

    data,_=decoder.raw_decode(text[min(starts):])
    return data

def load_all(table, columns):
    result=[]
    offset=0

    while True:
        rows=(sb.table(table)
              .select(columns)
              .range(offset,offset+999)
              .execute()
              .data)

        if not rows:
            break

        result.extend(rows)
        offset+=1000
        print("Loaded",len(result))

    return result


series=load_all(
    "series",
    "id,title,description,recommendation_tags,imdb_rating"
)

concepts=load_all(
    "concepts",
    "series_id,concept"
)

concept_map={}
for c in concepts:
    concept_map.setdefault(c["series_id"],[]).append(c["concept"])

for s in series:
    s["concepts"]=concept_map.get(s["id"],[])

print("TOTAL SERIES:",len(series))


intent_prompt=f'''
تو یک سیستم تحلیل نیاز کاربر برای پیشنهاد فیلم و سریال هستی.

جمله کاربر:

{USER_QUERY}


وظیفه تو این است که بفهمی کاربر از چه "تجربه انسانی" صحبت می‌کند، نه اینکه فقط کلمات را به ژانر تبدیل کنی.


خیلی مهم:
به عنوان مثال
اگر کاربر می‌گوید:
"من مریضم"
معنی آن این نیست که:
- سریال بیمارستانی می‌خواهد
- سریال پزشکی می‌خواهد
- داستان دکترها می‌خواهد


بلکه یعنی ممکن است دنبال این مفاهیم باشد:

- شخصیتی که خودش با بیماری روبه‌رو است
- کنار آمدن با بیماری
- امید در شرایط سخت
- ادامه دادن زندگی با وجود مشکل جسمی
- درمان و بهبود
- پذیرش شرایط


پس:

بیماری کاربر ≠ ژانر پزشکی
فوت عزیز یا فوت پدر یک کاربر مساوی نیست با سریال جنایی!



مثال:

کاربر:
"من مریضم و حالم خوب نیست"


تطابق تجربه انسانی:

The Big C
(شخصیت اصلی با بیماری زندگی می‌کند)

Daily Dose of Sunshine
(تجربه بیماران و سلامت روان)


اما:

Hospital Playlist
House M.D.
The Good Doctor


صرفاً چون بیمارستان یا پزشک دارند، تطابق مستقیم تجربه کاربر محسوب نمی‌شوند.


----------------------------


اول استخراج کن:

1) تجربه واقعی کاربر:

اگر وجود داشت:

type:
illness
grief
breakup
loneliness
financial_problem
mental_crisis
restart


description:
توضیح انسانی تجربه


story_requirements:
چیزهایی که باید در داستان پیدا شوند.


story_exclusions:
چیزهایی که نباید اشتباه گرفته شوند.


----------------------------


2) ترجیحات عمومی:

مثل:

حال خوب کن
امیدبخش
طنز
شخصیت دوست داشتنی
کشش داستانی
آرامش
خانوادگی
-یا هرچیز مشابه به تشخیص خودت.اما توهم نزن و از خودت نساز حتما شواهد براش توی پیام کاربر باشه!


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

'''

intent=clean_json(gemini_call(intent_prompt))
print(json.dumps(intent,ensure_ascii=False,indent=2))



candidates=[]

for i in range(0,len(series),150):

    batch=series[i:i+150]

    prompt=f'''
کاربر:
{USER_QUERY}

Intent:
{json.dumps(intent,ensure_ascii=False)}

از بین داده ها حداکثر 10 سریال انتخاب کن.

قانون بسیار مهم ⭐:

ستاره فقط وقتی true است که تجربه واقعی کاربر
در داستان، توضیحات، concept یا tag اثر وجود داشته باشد.

مثال بیماری:
The Big C = true
Daily Dose of Sunshine = true
Friends = false
Ted Lasso = false

حال خوب بودن به تنهایی دلیل ستاره نیست.


امتیاز:
experience_score حداکثر 40
preference_score حداکثر 60

فقط عنوان واقعی موجود را خروجی بده.

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
'''

    try:
        candidates.extend(clean_json(gemini_call(prompt)))
        print("BATCH",i,"OK")
    except Exception as e:
        print("ERROR",e)


db={}

for c in candidates:
    if c.get("title"):
        db[c["title"]]=c

candidates=list(db.values())

print("Candidates:",len(candidates))



ranking=clean_json(gemini_call(f'''
رتبه بندی نهایی انجام بده.

کاربر:
{USER_QUERY}

کاندیدها:
{json.dumps(candidates,ensure_ascii=False)}

قوانین:
- score نساز.
- فقط experience_score و preference_score نگه دار.
- IMDb دخالت نکند.
- star فقط برای تجربه مستقیم داستانی با حال و روز و تجربه واقعی کاربر باشد. و گرنه اصلا ستاره نذار

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
'''))

lookup={x["title"]:x for x in series}

for r in ranking:
    r["score"]=min(100,int(r.get("experience_score",0))+int(r.get("preference_score",0)))
    r["imdb_rating"]=lookup.get(r["title"],{}).get("imdb_rating")

ranking=[r for r in ranking if r["title"] in lookup]
ranking=sorted(ranking,key=lambda x:x["score"],reverse=True)[:100]

page=0

def next_page():
    global page

    end=min(page+10,len(ranking))

    for i,x in enumerate(ranking[page:end],page+1):
        print(("⭐ " if x.get("star") else "")+str(i)+" - "+x["title"])
        print("IMDb:",x["imdb_rating"])
        print("Score:",x["score"])
        print(x["reason"])
        print("---")

    page=end

    if page<len(ranking):
        print("برای ادامه next_page() را اجرا کنید")

print("READY",len(ranking))
next_page()


# Presentation addition: use series description as story summary in final Telegram output.
