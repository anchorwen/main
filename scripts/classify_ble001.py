#!/usr/bin/env python3
"""Classify BLE001 occurrences in hot-path files by risk category."""
import re, sys

def classify_file(filepath, label):
    with open(filepath, encoding='utf-8') as f:
        code_lines = f.readlines()

    # Use ruff to get line numbers
    import subprocess
    result = subprocess.run(
        ['ruff', 'check', '--select', 'BLE001', '--output-format', 'concise', filepath],
        capture_output=True, text=True, encoding='utf-8'
    )

    line_nums = []
    for line in result.stdout.split('\n'):
        m = re.search(r':(\d+):\d+:\s*BLE001', line)
        if m: line_nums.append(int(m.group(1)))

    cats: dict[str, list[tuple[int, str]]] = {'DEGRADE': [], 'FAIL_OPEN': [], 'FIRE_FORGET': [], 'STRATEGIC': []}

    for ln in sorted(set(line_nums)):
        if ln > len(code_lines): continue
        ctx_start = max(0, ln - 5)
        context = ''.join(code_lines[ctx_start:ln])
        except_line = code_lines[ln - 1].strip()

        # Classify
        has_log = any(w in context.lower() for w in ['logger', 'logging', 'print(', 'json.dumps'])
        has_fault = 'FaultTolerantContext' in context or 'log_and_continue' in context
        has_pass = 'pass' in except_line.lower() or 'pass' in code_lines[ln].strip().lower() if ln < len(code_lines) else False
        startup_ctx = any(w in context.lower() for w in ['startup', 'bootstrap', 'init', 'load_', 'hot_reload'])
        alert_ctx = any(w in context.lower() for w in ['alert', 'notify', 'tracker', 'recap', 'report', 'dashboard'])

        if has_fault:
            cat = 'DEGRADE'
        elif has_pass and alert_ctx:
            cat = 'FIRE_FORGET'
        elif has_pass and startup_ctx:
            cat = 'STRATEGIC'
        elif has_log:
            cat = 'DEGRADE'
        elif has_pass:
            cat = 'FAIL_OPEN'
        else:
            cat = 'DEGRADE'  # has some handling

        cats[cat].append((ln, except_line[:120]))

    print(f'\n{label} ({len(line_nums)} total):')
    for cat in ['DEGRADE', 'FAIL_OPEN', 'FIRE_FORGET', 'STRATEGIC']:
        items = cats[cat]
        print(f'  {cat}: {len(items)}')
        if cat == 'FAIL_OPEN':
            for ln, ex in items[:8]:
                print(f'    L{ln}: {ex}')

if __name__ == '__main__':
    classify_file('core/runtime/live_cycle.py', 'live_cycle.py')
    classify_file('scripts/live_intent_loop.py', 'live_intent_loop.py')
    classify_file('core/execution/strategy_line.py', 'strategy_line.py')
    classify_file('core/execution/execution_queue.py', 'execution_queue.py')
