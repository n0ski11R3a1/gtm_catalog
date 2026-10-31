# GTM Distribution ERP — Phase 1 (v0.1)

Flask + SQLite web app. ပါဝင်တာ:
- Login + အဆင့် ၄ မျိုး (Owner / Warehouse / Sales / Finance) — Actual Base (အရင်းအမှန်) ကို Owner + Finance ပဲ မြင်ရ
- Products: ဈေး ၄ မျိုး (Base / Actual / Retail / Wholesale / B2C မှတ်ရန်), margin အလိုအလျောက်, ပုံ
- Stock အရေအတွက် + မှတ်တမ်း (ledger) — ရွေ့တိုင်း ဘယ်သူ/ဘာကြောင့် မှတ်ထားတယ်
- အဝယ် ၂ ဆင့်: ဂိုထောင် သွင်း (စစ်ရန်ကျန်) → Finance/Ko စစ်ပြီး (Base Price update လုပ်လို့ရ)
- အရောင်း: Rep က လက်လီ/လက်ကား ကိုယ်တိုင် ရွေး, approval မလို, စုစုပေါင်းပေါ် လျှော့ဈေး (flat Ks), status အရိုးရှင်း (new → ပို့ပြီး/ငွေရ), ပို့ရင် stock အလိုအလျောက် နှုတ်
- Invoice: A5 portrait print (browser print → PDF)
- Returns/Damage: အကောင်း → stock ပြန်ဝင် + တန်ဖိုး မှတ်; ပျက်စီး → အကြောင်းအရင်း မဖြစ်မနေ + Finance စစ်
- Sales Catalog (PWA): offline ရတယ် — catalog+ဈေးတွေ ဖုန်းထဲ cache, order ကို ဖုန်းထဲ တန်းစီ, လိုင်း ပြန်ရရင် auto sync; stock ထက် ပိုတဲ့ order ဆို ⚠️ flag
- Finance အတွက် flag: Wholesale အောက် / အရင်းအမှန် အောက် ရောင်းထားတဲ့ invoice တွေ အလိုအလျောက် ပြတယ်

## Data
data/gtm.db ထဲမှာ ပြင်ပြီးသား Master ကနေ ရွေ့ထားတဲ့ ပစ္စည်း ၃၅၅ မျိုး, ပုံ ၁၄၄ ပုံ, နေရာ ၅၅ ခု ပါပြီး။
Stock အရေအတွက်တွေက 0 က စတယ် — go-live မှာမှာ မြေပြင် အရေအတွက် (Opening Balance) ထည့်ရမယ် (Products စာမျက်နှာမှာ Stock ပြင် နဲ့ ထည့်လို့ရ)။

## PythonAnywhere မှာ တင်နည်း (အခမဲ့ အကောင့်)
1. pythonanywhere.com မှာ အကောင့် ဖွင့် (username = လိပ်စာ ဖြစ်မယ် — ပြောင်းမရ, brand နာမည် သေချာရွေး ဥပမာ gtmdistribution)
2. Files → upload: ဒီ folder တစ်ခုလုံး (gtm-erp-app.zip ကို upload ပြီး Bash console မှာ unzip)
3. Web → Add a new web app → Manual configuration → Python 3 (နောက်ဆုံး)
4. WSGI file ကို ပြင်: Source code path = /home/<username>/gtm-erp-app, working directory လည်း အတူတူ, WSGI ထဲမှာ:
   import sys; sys.path.insert(0, '/home/<username>/gtm-erp-app')
   from app import app as application
5. Reload → site ပွင့်ပြီ။ ပထမဆုံး ဝင်ရင် /setup မှာ Owner account ကိုယ်တိုင် ဆောက် (password ကိုယ်ပဲ သိမယ်)
6. GTM_SECRET environment ကို Web tab မှာ လျှို့ဝှက် စာသား တစ်ခု ထားပေးရင် ပိုလုံခြုံတယ်

## မှတ်ချက်များ (v0.1)
- Returns "နှုတ်ပေးရမယ့် တန်ဖိုး" က မှတ်တမ်း အဖြစ် သိမ်းတာ — order စုစုပေါင်းကို အလိုအလျောက် မပြင်ဘူး (Finance က စစ်ရင်း ကိုင်တွယ်ရန်)
- နေ့တိုင်း backup: data/gtm.db ဖိုင်ကို download လုပ်ပြီး သိမ်းထား (သို့) Owner အတွက် export ခလုတ် နောက် version မှာ ထည့်မယ်
