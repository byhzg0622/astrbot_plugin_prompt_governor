import ast
ast.parse(open('tests/test_core.py', encoding='utf-8').read())
print('test_core syntax OK')
ast.parse(open('tests/test_hooks.py', encoding='utf-8').read())
print('test_hooks syntax OK')
ast.parse(open('tests/test_webapi.py', encoding='utf-8').read())
print('test_webapi syntax OK')
