"""RAG 评估体系：检索层（recall@k）+ 生成层（引用核查 + 忠实度裁判）+ 负例 + badcase 回流。

设计文档：docs/RAG知识库系统设计.md（§10）。三道防线对应三种失败：
  1. 检索层：搜没搜到权威出处（recall@k）
  2. 引用核查：答案标的课号是否真实来自检索结果（防编造出处，纯程序判定）
  3. 忠实度裁判：答案内容是否忠于检索片段（LLM 评审，可选 --judge）
负例（negative 题）验证"知识库没有就说没有"的纪律，防止编造。

用法：
    python -m rag.evals                    # 检索层评估（快，不调模型）
    python -m rag.evals full [--limit 5] [--judge]   # 检索层 + 生成层
    python -m rag.evals add "问题文本" --expect 61,62 [--neg]   # badcase 回流
    python -m rag.evals history            # 历史基线对比
"""
import json
import re
import sys
import time
from pathlib import Path

from rag import config
from rag.retrieve import search

BANK_PATH = Path(__file__).parent / "eval_bank.json"
HISTORY_PATH = config.ROOT / "data" / "rag_corpus" / "eval_history.jsonl"

# 负例判定：top1 相似度低于此阈值视为"检索不到"（正例命中普遍 >0.6，实测后定）
NEG_SCORE_THRESHOLD = 0.55

# 引用核查的课号提取：答案中的「第X课」
CITE_RE = re.compile(r"第\s*(\d{1,3})\s*课")

# 负例回答的"如实拒答"关键词
REFUSE_RE = re.compile(r"没有相关|查不到|无法|没有找到|未检索|不属于|不在知识库|没有找到相关")


# ============ 检索层 ============

def eval_retrieval(bank):
    """召回评估：positive 题 recall@1/@3；negative 题 top1 分数应低于阈值。"""
    hits1 = hits3 = 0
    npos = nneg = neg_ok = 0
    rows = []
    for e in bank:
        if e.get('negative'):
            nneg += 1
            got = search(e['q'], top_k=1)
            top = got[0]['score'] if got else 0.0
            ok = top < NEG_SCORE_THRESHOLD
            neg_ok += ok
            rows.append({'id': e['id'], 'q': e['q'], 'negative': True,
                         'top_score': top, 'pass': ok,
                         'top_lesson': got[0]['lesson'] if got else None})
            continue
        npos += 1
        got = [h['lesson'] for h in search(e['q'], top_k=3)]
        expect = set(e['expect_lessons'])
        ok1, ok3 = bool(set(got[:1]) & expect), bool(set(got[:3]) & expect)
        hits1 += ok1
        hits3 += ok3
        rows.append({'id': e['id'], 'q': e['q'], 'expect': sorted(expect),
                     'got': got, 'pass': ok1 or ok3, 'pass1': ok1})
    return {'recall1': hits1 / max(npos, 1), 'recall3': hits3 / max(npos, 1),
            'neg_pass': neg_ok / max(nneg, 1), 'rows': rows,
            'n_pos': npos, 'n_neg': nneg}


def print_retrieval(rep):
    print(f"—— 检索层 ——")
    print(f"positive {rep['n_pos']} 题 | recall@1 = {rep['recall1']:.0%} | recall@3 = {rep['recall3']:.0%}")
    print(f"negative {rep['n_neg']} 题 | 如实拒答率 = {rep['neg_pass']:.0%}（top1 分数 < {NEG_SCORE_THRESHOLD}）")
    for r in rep['rows']:
        if r.get('negative'):
            mark = '✓' if r['pass'] else '✗'
            print(f"{mark} [{r['id']}] {r['q']} → top1={r['top_score']:.2f}"
                  + (f"（误命中第{r['top_lesson']}课）" if not r['pass'] else ''))
        else:
            mark = '✓' if (r['pass'] and r['pass1']) else ('~' if r['pass'] else '✗')
            print(f"{mark} [{r['id']}] {r['q']} 期望={r['expect']} 实际={r['got']}")


# ============ 生成层（引用核查 + 忠实度裁判） ============

QA_SYSTEM = ('你是缠论知识助理。必须先调 kb_search 检索知识库（可换词多检索几次）。'
             '知识库双层：缠论 108 课原文（出处=第几课）+ 本系统私有口径（出处=类别与日期）。'
             '只依据检索到的片段作答并标注出处；'
             '检索不到相关内容时如实说"知识库没有相关内容"，不许凭印象编造。300 字以内。')

JUDGE_SYSTEM = ('你是评估裁判。给你检索片段和一个回答，判断回答是否忠实于片段：'
                '结论是否都能在片段中找到依据、有没有添加片段外的事实。'
                '只输出 JSON，不要多余文字：{"faithful": true/false, "issues": ["具体问题"]}。'
                '回答声称"没有相关内容"且片段确实没有 → faithful=true。')


def eval_generation(bank, limit=None, judge=False):
    """生成层评估：跑完整问答管线，程序核查引用真实性 + 可选 LLM 裁判忠实度。"""
    try:
        import agent.llm as L
        import agent.tools as T
    except Exception as e:
        print(f'生成层评估跳过：agent 包不可用（{e}）')
        return None
    if not L.get_key():
        print('生成层评估跳过：LLM 未配置（secret.py 缺 LLM_API_KEY）')
        return None

    schemas, executors = T.kb_qa_registry()
    positives = [e for e in bank if not e.get('negative')]
    if limit:
        positives = positives[:limit]
    print(f"\n—— 生成层 ——（{len(positives)} 题，引用核查{' + 忠实度裁判' if judge else ''}）")

    rows, n_cite, n_fabricate, n_faithful = [], 0, 0, 0
    for e in positives:
        # 模型自主检索：包装执行器，记录它实际查到的课与片段（引用真实性以此为基准）
        seen_lessons = set()
        seen_snippets = {}

        def tool_fn(name, args):
            out = executors[name](args)
            if name == 'kb_search':
                for r in (out.get('rows') or []):
                    m = re.search(r'第\s*(\d+)\s*课', r.get('source') or '')
                    if m:
                        ln = int(m.group(1))
                        seen_lessons.add(ln)
                        seen_snippets.setdefault(ln, r.get('snippet') or '')
            return out

        try:
            res = L.chat_tools(QA_SYSTEM, [{'role': 'user', 'content': e['q']}],
                               schemas, tool_fn)
            answer = res['text']
        except Exception as ex:
            rows.append({'id': e['id'], 'q': e['q'], 'error': str(ex)[:120]})
            continue
        cited = {int(x) for x in CITE_RE.findall(answer)}
        fabricated = sorted(cited - seen_lessons)
        has_cite = bool(cited)
        n_cite += has_cite
        n_fabricate += bool(fabricated)
        row = {'id': e['id'], 'q': e['q'], 'cited': sorted(cited),
               'retrieved': sorted(seen_lessons), 'fabricated': fabricated,
               'has_citation': has_cite, 'tool_calls': res.get('tool_calls', 0),
               'answer_head': answer[:120]}
        if judge:
            try:
                snippets = '\n'.join(f"(第{ln}课) {seen_snippets[ln][:300]}"
                                     for ln in sorted(seen_snippets))
                jtxt = L.chat(JUDGE_SYSTEM, f"【检索片段】\n{snippets}\n\n【回答】\n{answer}")
                m = re.search(r'\{.*\}', jtxt, re.S)
                j = json.loads(m.group(0)) if m else {}
                faithful = bool(j.get('faithful'))
                row['faithful'] = faithful
                row['issues'] = j.get('issues') or []
                n_faithful += faithful
            except Exception as ex:
                row['judge_error'] = str(ex)[:120]
        rows.append(row)
        fb = f" ⚠️编造{fabricated}" if fabricated else ''
        fth = f" 忠实={'✓' if row.get('faithful') else '✗'}" if 'faithful' in row else ''
        iss = ('｜' + ';'.join(row['issues'][:2])) if row.get('issues') else ''
        jerr = f" 裁判失败={row['judge_error'][:60]}" if 'judge_error' in row else ''
        print(f"[{e['id']}] 引用={sorted(cited) or '无'}{fb}{fth}{jerr}{iss}")

    n = len([r for r in rows if 'error' not in r])
    n_judged = len([r for r in rows if 'faithful' in r])
    out = {'n': n, 'cite_rate': n_cite / max(n, 1),
           'fabricate_rate': n_fabricate / max(n, 1),
           'faithful_rate': (n_faithful / max(n_judged, 1)) if judge and n_judged else None,
           'n_judged': n_judged, 'rows': rows}
    print(f"引用覆盖率 = {out['cite_rate']:.0%} | 编造出处率 = {out['fabricate_rate']:.0%}"
          + (f" | 忠实率 = {out['faithful_rate']:.0%}" if judge else ""))
    return out


# ============ 题库管理与历史 ============

def load_bank():
    return json.loads(BANK_PATH.read_text(encoding='utf-8'))['items']


def add_case(q, expect, negative=False):
    bank = load_bank()
    next_id = f"N{len([e for e in bank if e.get('negative')]) + 1:02d}" if negative \
        else f"Q{len([e for e in bank if not e.get('negative')]) + 1:02d}"
    bank.append({'id': next_id, 'q': q, **({'negative': True} if negative
                                           else {'expect_lessons': expect})})
    BANK_PATH.write_text(json.dumps({'_doc': load_bank_doc(), 'items': bank},
                                    ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"已回流 {next_id}: {q}" + (f" → 期望 {expect}" if expect else "（负例）"))


def load_bank_doc():
    return json.loads(BANK_PATH.read_text(encoding='utf-8')).get('_doc', '')


def save_history(summary):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + '\n')
    print(f"已记录基线：{HISTORY_PATH}")


def show_history():
    if not HISTORY_PATH.exists():
        print('暂无历史记录')
        return
    for line in HISTORY_PATH.read_text(encoding='utf-8').splitlines():
        h = json.loads(line)
        gen = ''
        if h.get('generation'):
            g = h['generation']
            gen = f" | 引用覆盖 {g['cite_rate']:.0%} 编造 {g['fabricate_rate']:.0%}"
            if g.get('faithful_rate') is not None:
                gen += f" 忠实 {g['faithful_rate']:.0%}"
        print(f"{h['ts']}  recall@1={h['retrieval']['recall1']:.0%} "
              f"recall@3={h['retrieval']['recall3']:.0%} "
              f"拒答={h['retrieval']['neg_pass']:.0%}{gen}  ({h.get('note', '')})")


# ============ 入口 ============

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else 'retrieval'

    if cmd == 'add':
        q = args[1] if len(args) > 1 else ''
        expect = []
        neg = '--neg' in args
        if '--expect' in args:
            expect = [int(x) for x in args[args.index('--expect') + 1].split(',')]
        if not q:
            print('用法：python -m rag.evals add "问题" --expect 61,62 [--neg]')
            return
        add_case(q, expect, neg)
        return
    if cmd == 'history':
        show_history()
        return

    note = ''
    bank = load_bank()
    rep = eval_retrieval(bank)
    print_retrieval(rep)

    gen = None
    if cmd == 'full':
        limit = int(args[args.index('--limit') + 1]) if '--limit' in args else None
        judge = '--judge' in args
        gen = eval_generation(bank, limit=limit, judge=judge)
        note = 'full' + ('+judge' if judge else '')

    save_history({'ts': time.strftime('%Y-%m-%d %H:%M'),
                  'retrieval': {'recall1': round(rep['recall1'], 3),
                                'recall3': round(rep['recall3'], 3),
                                'neg_pass': round(rep['neg_pass'], 3)},
                  'generation': ({'cite_rate': round(gen['cite_rate'], 3),
                                  'fabricate_rate': round(gen['fabricate_rate'], 3),
                                  'faithful_rate': (round(gen['faithful_rate'], 3)
                                                    if gen['faithful_rate'] is not None else None)}
                                 if gen else None),
                  'model_note': note})


if __name__ == '__main__':
    main()
