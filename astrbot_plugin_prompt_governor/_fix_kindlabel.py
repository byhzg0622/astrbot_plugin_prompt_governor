# -*- coding: utf-8 -*-
from pathlib import Path
p = Path('astrbot_plugin_prompt_governor/pages/governor/index.html')
text = p.read_text(encoding='utf-8')
old = '  var kindLabel = b.tag ? "&lt;" + esc(b.tag) + "&gt;" :
    (b.kind === "skills" ? "技能注入" : (b.kind === "tool_prompt" ? "工具说明" : (b.kind === "tool" ? "工具注入" : esc(b.kind))));'
new = '''  var kindLabel;
  if (b.kind === "tool") kindLabel = "工具注入";
  else if (b.kind === "skills") kindLabel = "技能注入";
  else if (b.kind === "tool_prompt") kindLabel = "工具说明";
  else if (b.tag) kindLabel = "&lt;" + esc(b.tag) + "&gt;";
  else kindLabel = esc(b.kind);'''
if old in text:
    text = text.replace(old, new)
    p.write_text(text, encoding='utf-8')
    print('kindLabel fixed')
else:
    print('old not found')
    idx = text.index('var kindLabel')
    print(repr(text[idx:idx+200]))
