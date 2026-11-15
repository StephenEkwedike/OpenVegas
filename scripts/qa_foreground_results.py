"""Accept exactly two complete foreground cases; publish only closed labels and counters."""
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

STAGES = ('isolation','fixture-canonical','fixture-configure','conpty','native-start','composer',
          'status-0','answer-0','switch-v','status-1','answer-1','switch-host','status-2','answer-2',
          'shutdown','supplier-receipts')
COUNTS = ('supplier_answers','bank_http','bank_issues','bank_revokes')
LABELS = {'managed-ownership-selection-routing-refused','managed-billing-unavailable','pinned-host-entry-not-found',
          'child-exited','no-reply','trust-dialog',
          'login-dialog','api-key-dialog','startup-dialog','git-bash-error','native-option-error',
          'switch-busy','switch-held','switch-refused','native-route','native-inventory','native-root',
          'native-model','native-workspace','native-fence','session-lease','command-unavailable',
          'wallet-prepare','wallet-outcome','request-boundary','supplier-not-reached','supplier-protocol',
          'control-as-prompt','answer-not-rendered','unsolicited-request','prompt-shape','exit-timeout',
          'exit-nonzero','bank-receipt','launcher-cleanup','receipt-write','bridge-at-exit',
          'session-shutdown','native-process-exit','console-restore','grant-close'}


def report(file):
    if not file.is_file() or not 0 < file.stat().st_size <= 4*1024*1024:
        return False
    cases = list(ET.parse(file).getroot().iter('testcase'))
    ok = len(cases) == 2
    for host in ('codex','claude'):
        name = 'test_installed_customer_switch['+host+']'
        matches = [c for c in cases if c.get('name') == name and c.get('classname') ==
                   'tests.test_integrations.test_windows_installed_switch']
        if len(matches) != 1:
            print('::error::'+host+' no unique named foreground result')
            ok = False
            continue
        c = matches[0]
        def props(n):
            return [v.get('value') for v in c.findall("./properties/property[@name='"+n+"']")]
        history = props('qa_stage')
        label = props('qa_failure_label')
        counts = {}
        for n in COUNTS:
            v = props(n)
            if len(v) == 1 and v[0] in {str(k) for k in range(101)}:
                counts[n] = int(v[0])
        good = (not any(c.find(t) is not None for t in ('failure','error','skipped'))
                and history == list(STAGES) and not label and counts.keys() == set(COUNTS)
                and counts['supplier_answers'] == 3 and counts['bank_issues'] == counts['bank_revokes'] == 1
                and counts['bank_http'] >= 7)
        ok &= good
        fields = [host, 'PASS' if good else 'NOT-PASSED',
                  'stage='+(history[-1] if history and history[-1] in STAGES else 'unobserved'),
                  'reason='+(label[0] if len(label) == 1 and label[0] in LABELS else 'unclassified')]
        fields += [key+'='+str(counts.get(key,'unobserved')) for key in COUNTS]
        print(('::notice::' if good else '::error::')+' '.join(fields))
    return ok


if __name__ == '__main__':
    try:
        sys.exit(0 if report(Path(os.environ['RUNNER_TEMP']) / 'customer.xml') else 1)
    except (ET.ParseError, OSError, ValueError):
        sys.exit('No usable Windows foreground evidence')
