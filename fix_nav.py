# -*- coding: utf-8 -*-
# 在 DASHBOARD_JS 主块内应用导航重构（之前误改到 HTML 内嵌副本）
path = "valuation_app/static.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

i_start = content.index("DASHBOARD_JS = r'''") + len("DASHBOARD_JS = r'''")
i_end = content.index("'''", i_start)
js = content[i_start:i_end]

# 1. DASH_PAGES 加 labels
old = "const DASH_PAGES={overview:'投资总览',core:'核心汇报',returns:'收益分析',market:'市场与基准',lab:'策略实验室',strategy:'策略组合',risk:'风险管理',underlying:'底层分析',barra:'多因子分析',attribution:'持仓变动归因（估算）',other:'其他管理',knowledge:'知识库',data:'数据与说明'};"
new = "const DASH_PAGES={overview:'投资总览',core:'核心汇报',returns:'收益分析',market:'市场与基准',labels:'产品标签',strategy:'策略组合',risk:'风险管理',underlying:'底层分析',barra:'多因子分析',attribution:'持仓变动归因（估算）',other:'其他管理',lab:'策略实验室',knowledge:'知识库',data:'数据与说明'};"
assert js.count(old) == 1, "DASH_PAGES in js block: %d" % js.count(old)
js = js.replace(old, new)

# 2. dashIcon
old = "function dashIcon(k){return{overview:'◫',core:'▣',returns:'↗',market:'⌁',lab:'⚗',strategy:'◉',risk:'◇',underlying:'⌘',barra:'β',attribution:'∑',other:'▤',knowledge:'▧',data:'⚙'}[k]}"
new = "function dashIcon(k){return{overview:'◫',core:'▣',returns:'↗',market:'⌁',labels:'◈',strategy:'◉',risk:'◇',underlying:'⌘',barra:'β',attribution:'∑',other:'▤',lab:'⚗',knowledge:'▧',data:'⚙'}[k]}"
assert js.count(old) == 1, "dashIcon in js block"
js = js.replace(old, new)

# 3. main 数组（js 主块内的版本——含 lab 的那个）
old = "const main=['overview','core','returns','market','lab','strategy','risk','underlying','barra','attribution'];"
if js.count(old) == 1:
    js = js.replace(old, "const main=['overview','core','returns','market','labels','strategy','risk','underlying','barra','attribution'];")
    print("main 数组已改（lab 移除，labels 加入）")
else:
    # 旧版无 lab 的
    old2 = "const main=['overview','core','returns','market','strategy','risk','underlying','barra','attribution'];"
    assert js.count(old2) == 1, "main array not found in js block: %d/%d" % (js.count(old), js.count(old2))
    js = js.replace(old2, "const main=['overview','core','returns','market','labels','strategy','risk','underlying','barra','attribution'];")
    print("main 数组已改（旧版路径）")

# 4. 侧栏模板：系统与数据组加 知识库 + 策略实验室（js 块内）
old = "<label>系统与数据</label><button class=dash-nav data-page=data onclick=\"dashGo('data')\"><i>⚙</i><span>数据与说明</span></button>"
new = "<label>系统与数据</label><button class=dash-nav data-page=knowledge onclick=\"dashGo('knowledge')\"><i>▧</i><span>知识库</span></button><button class=dash-nav data-page=lab onclick=\"dashGo('lab')\"><i>⚗</i><span>策略实验室</span></button><button class=dash-nav data-page=data onclick=\"dashGo('data')\"><i>⚙</i><span>数据与说明</span></button>"
if js.count(old) == 1:
    js = js.replace(old, new)
    print("侧栏系统组已改")
else:
    print("侧栏已是新结构（count=%d）" % js.count(old))

# 5. 容器
old = "k==='lab'?'<div id=dashLab></div>'"
new = "k==='labels'?'<div id=dashLabels></div>':k==='lab'?'<div id=dashLab></div>'"
assert js.count(old) == 1, "container in js block"
js = js.replace(old, new)

# 6. 路由
old = "if(page==='knowledge')renderKnowledge();if(page==='data')renderDashData();"
new = "if(page==='knowledge')renderKnowledge();if(page==='labels')renderLabelEditorPage();if(page==='data')renderDashData();"
assert js.count(old) == 1, "route in js block"
js = js.replace(old, new)

content = content[:i_start] + js + content[i_end:]
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("DASHBOARD_JS 主块导航重构完成")
