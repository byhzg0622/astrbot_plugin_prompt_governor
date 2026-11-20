import sys
sys.path.insert(0, '.')
sys.path.insert(0, '..')
import unittest
loader = unittest.TestLoader()
suite = unittest.TestSuite()
for modname in ['astrbot_plugin_prompt_governor.tests.test_core',
                'astrbot_plugin_prompt_governor.tests.test_hooks',
                'astrbot_plugin_prompt_governor.tests.test_webapi']:
    suite.addTests(loader.loadTestsFromName(modname))
runner = unittest.TextTestRunner(verbosity=1, stream=open('_tests7_out.txt','w',encoding='utf-8'))
result = runner.run(suite)
with open('_tests7_out.txt','a',encoding='utf-8') as f:
    f.write(f'\nRESULT {result.wasSuccessful()} {len(result.failures)} {len(result.errors)}\n')
for t, tb in result.failures:
    with open('_tests7_out.txt','a',encoding='utf-8') as f:
        f.write('\nFAIL ' + str(t) + '\n' + str(tb) + '\n')
for t, tb in result.errors:
    with open('_tests7_out.txt','a',encoding='utf-8') as f:
        f.write('\nERR ' + str(t) + '\n' + str(tb) + '\n')
sys.exit(0 if result.wasSuccessful() else 1)
