"""Offline UI regressions. Run: python3 -m unittest discover -s bin -p 'test_*.py'.

Breaks caught: default/explicit effort lost at CLI; selector drift; max=Pro
assumption; substring effort matching; hidden model radios counted as effort;
unchecked selection; id-less composer attachment scope. No external requests.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

SPEC = importlib.util.spec_from_file_location("review", Path(__file__).with_name("pack_and_ask.py"))
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def slider_html(labels=None, initial=0, legacy=False, stuck=False, stale=False):
    labels = labels or ["Instant", "Medium", "High", "Extra High", "Pro"]
    trigger = ('class="__composer-pill" aria-haspopup="menu"' if legacy else
               'aria-label="ChatGPT 모델 선택" data-codex-intelligence-trigger="true"')
    popup = 'data-testid="composer-intelligence-picker-content"' if legacy else ''
    control = 'data-model-reasoning-effort-slider' if legacy else 'data-model-picker-power-slider'
    return f'''<button {trigger} id="trigger" data-selected-reasoning-effort="medium"></button>
<div id="menu" role="menu" data-state="closed" {popup} hidden>
 <div role="menuitem" aria-label="모델 선택" data-model-picker-view-toggle="true" id="model-label">6 Pro</div>
 <div role="status" id="status"></div>
 <div role="menuitem" aria-label="파워" data-reasoning-slider="true" tabindex="0" id="power">
  <span {control}><span role="slider" aria-hidden="true" aria-valuemin="0" id="slider"></span></span>
 </div>
 <div inert aria-hidden="true" style="opacity:0;position:absolute">
  <div role="menuitemradio" aria-checked="true">Latest</div>
  <div role="menuitemradio" aria-checked="false">GPT-5.6 Sol</div>
  <div role="menuitemradio" aria-checked="false">GPT-5.5</div>
 </div>
</div>
<script>
const labels={json.dumps(labels)}, initial={initial}; let idx=initial;
const trigger=document.getElementById('trigger'),menu=document.getElementById('menu'),
 statusEl=document.getElementById('status'),slider=document.getElementById('slider');
function paint(){{
 slider.setAttribute('aria-valuenow',idx);slider.setAttribute('aria-valuemax',labels.length-1);
 statusEl.textContent=labels[{('initial' if stale else 'idx')}] + ', '+labels.length+'개 중 '+(idx+1)+'번째.';
 trigger.textContent=labels[{('initial' if stale else 'idx')}];
 document.getElementById('model-label').textContent=idx===4?'6 Pro':labels[idx];
}}
trigger.onclick=()=>{{menu.hidden=!menu.hidden;menu.dataset.state=menu.hidden?'closed':'open';}};
document.getElementById('power').onkeydown=e=>{{
 if({str(stuck).lower()}) return;
 if(e.key==='ArrowRight')idx=Math.min(labels.length-1,idx+1);
 if(e.key==='ArrowLeft')idx=Math.max(0,idx-1);
 paint();
}};
document.onkeydown=e=>{{if(e.key==='Escape'){{menu.hidden=true;menu.dataset.state='closed';}}}};
paint();
</script>'''


def radio_html(labels=None):
    labels = labels or ["즉시", "중간", "높음", "매우 높음", "Pro"]
    items = ''.join(f'<div role="menuitemradio" tabindex="0" aria-checked="false">{x}</div>' for x in labels)
    return f'''<button class="__composer-pill" aria-haspopup="menu" id="trigger">Pro</button>
<div role="menu" data-state="closed" id="menu" hidden>{items}
<div role="menuitemradio" aria-checked="true">GPT-5.6 Sol</div>
<div role="menuitemradio" aria-checked="true">Latest</div></div>
<script>
const menu=document.getElementById('menu'),trigger=document.getElementById('trigger');
trigger.onclick=()=>{{menu.hidden=!menu.hidden;menu.dataset.state=menu.hidden?'closed':'open';}};
for(const item of menu.children) item.onclick=()=>{{
 for(const x of menu.children) x.setAttribute('aria-checked','false');
 item.setAttribute('aria-checked','true');trigger.textContent=item.textContent;
 menu.hidden=true;menu.dataset.state='closed';
}};
document.onkeydown=e=>{{if(e.key==='Escape'){{menu.hidden=true;menu.dataset.state='closed';}}}};
</script>'''


def response_html(turn='0', uuid='answer-uuid', copy=True, label='복사', body='새 답변'):
    button = f'<button aria-label="{label}">copy</button>' if copy else ''
    return f'''<div class="group flex flex-col" data-chatgpt-search-message-ids="user-{turn}">
<div data-content-search-unit-key="fallback-turn-{turn}:0:user"><h4 data-conversation-role="user">나의 말:</h4>
<div>사용자 질문 오염금지</div><button aria-label="메시지 복사">user copy</button></div>
<div><div><div data-content-search-unit-key="fallback-turn-{turn}:1:assistant"
 data-chatgpt-search-unit-key="fallback-turn-{turn}:1:assistant" data-chatgpt-search-message-ids="{uuid} extra-{uuid}">
<h4 data-conversation-role="assistant">ChatGPT 답변:</h4>
<div data-chatgpt-selection-message-id="{uuid}"><div data-markdown-text-style="assistant-message">{body}</div></div>
</div></div></div>{button}</div>'''


class EffortUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(500)
        # Only remove production polling delays; DOM events and selectors stay real.
        self.clock = patch.object(review, 'time', types.SimpleNamespace(sleep=lambda _: None))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.context.close()

    def load(self, html):
        self.page.goto('about:blank')
        self.page.set_content(html)

    def parse_cli(self, argv):
        original = argparse.ArgumentParser.parse_args
        parsed = []

        class Parsed(Exception):
            pass

        def capture(parser, *args, **kwargs):
            try:
                parsed.append(original(parser, argv))
            except SystemExit as exc:
                self.fail(f'CLI rejected requested effort: {argv} (exit {exc.code})')
            raise Parsed

        with patch.object(argparse.ArgumentParser, 'parse_args', capture):
            with self.assertRaises(Parsed):
                review.main()
        return parsed[0]

    def assert_selection(self, want, expected):
        ok, name = review.select_model(self.page, want)
        self.assertTrue(ok, name)
        self.assertEqual(self.page.locator('#trigger').inner_text(), expected)
        self.assertNotIn('GPT-5.6', name)  # hidden alternatives are not the active model

    def test_new_response_counts_ids_and_body(self):
        self.load(response_html())
        self.assertEqual(review.count_msgs_strict(self.page, review.USER_MSG_SELECTORS), 1)
        self.assertEqual(review.count_msgs_strict(self.page, review.ASSISTANT_MSG_SELECTORS), 1)
        self.assertTrue({'user-0', 'answer-uuid', 'extra-answer-uuid'} <= review.msg_id_set(self.page))
        self.assertEqual(review.new_assistant_text(self.page, set()), '새 답변')
        self.assertEqual(review.last_assistant_text(self.page), '새 답변')

    def test_response_identity_excludes_old_answer_after_wrapper_index_changes(self):
        self.load(response_html(turn='8', uuid='old'))
        self.assertIsNone(review.new_assistant_node(self.page, {'old', 'extra-old'}))
        self.load(response_html(turn='8', uuid='old') + response_html(turn='9', uuid='new', body='새 UUID 답'))
        self.assertEqual(review.new_assistant_text(self.page, {'old', 'extra-old'}), '새 UUID 답')

    def test_new_response_copy_belongs_to_assistant_turn(self):
        for label in ['복사', 'Copy']:
            with self.subTest(label=label):
                self.load(response_html(label=label))
                node = review.new_assistant_node(self.page, set())
                self.assertIsNotNone(node)
                self.assertTrue(review.turn_terminal(self.page, node))
                self.assertEqual(review.node_copy_button(node).get_attribute('aria-label'), label)

    def test_user_copy_and_ready_send_do_not_finish_new_assistant(self):
        self.load(response_html(copy=False) + '<button aria-label="보내기">send</button>')
        node = self.page.query_selector('[data-content-search-unit-key$=":assistant"]')
        self.assertIsNone(review.node_copy_button(node))
        self.assertFalse(review.turn_terminal(self.page, node))

    def test_other_turn_copy_cannot_finish_new_answer(self):
        self.load(response_html(uuid='old') + response_html(turn='1', uuid='new', copy=False))
        node = self.page.query_selector('[data-chatgpt-selection-message-id="new"]')
        self.assertIsNone(review.node_copy_button(node))
        self.assertFalse(review.turn_terminal(self.page, node))

    def test_response_without_uuid_is_not_new_from_fallback_index(self):
        html = response_html().replace('data-chatgpt-selection-message-id="answer-uuid"', '').replace(
            'data-chatgpt-search-message-ids="answer-uuid extra-answer-uuid"', '')
        self.load(html)
        self.assertIsNone(review.new_assistant_node(self.page, set()))

    def test_old_response_identity_body_and_copy(self):
        self.load('<section data-turn="assistant"><div data-message-author-role="assistant" data-message-id="old-ui">'
                  '기존 답변</div><button data-testid="copy-turn-action-button">Copy</button></section>')
        self.assertEqual(review.new_assistant_text(self.page, set()), '기존 답변')
        self.assertTrue(review.turn_terminal(self.page, review.new_assistant_node(self.page, set())))
        self.assertIsNone(review.new_assistant_node(self.page, {'old-ui'}))

    def test_cli_omission_actually_selects_pro(self):
        self.load(slider_html())
        args = self.parse_cli([])
        self.assertIsNotNone(args.model, 'unspecified effort must not skip verification')
        self.assert_selection(args.model, 'Pro')

    def test_cli_explicit_effort_is_not_overwritten(self):
        for flag, value, expected in [('--effort', '중간', 'Medium'), ('--model', 'high', 'High')]:
            with self.subTest(flag=flag):
                self.load(slider_html(initial=4))
                self.assert_selection(self.parse_cli([flag, value]).model, expected)

    def test_cli_unknown_effort_rejected_before_execution(self):
        original = argparse.ArgumentParser.parse_args
        for value in ['turbo', '']:
            with self.subTest(value=value):
                def capture(parser, *args, **kwargs):
                    return original(parser, ['--effort', value, '--harvest', 'invalid'])
                with patch.object(argparse.ArgumentParser, 'parse_args', capture):
                    with self.assertRaises(SystemExit) as raised:
                        review.main()
                self.assertEqual(raised.exception.code, 2)

    def test_conflicting_cli_aliases_are_rejected(self):
        original = argparse.ArgumentParser.parse_args
        def capture(parser, *args, **kwargs):
            return original(parser, ['--model', 'pro', '--effort', 'high', '--harvest', 'invalid'])
        with patch.object(argparse.ArgumentParser, 'parse_args', capture):
            with self.assertRaises(SystemExit) as raised:
                review.main()
        self.assertEqual(raised.exception.code, 2)

    def test_new_slider_aliases_and_exact_meaning(self):
        for request, label in [('pro', 'Pro'), ('즉시', 'Instant'), ('instant', 'Instant'),
                               ('중간', 'Medium'), ('medium', 'Medium'), ('standard', 'Medium'),
                               ('높음', 'High'), ('high', 'High'), ('매우 높음', 'Extra High'),
                               ('extra high', 'Extra High'), ('very high', 'Extra High'),
                               ('xhigh', 'Extra High'), ('extended', 'Extra High')]:
            with self.subTest(request=request):
                self.load(slider_html(initial=4))
                self.assert_selection(request, label)

    def test_english_switcher(self):
        self.load(slider_html().replace('ChatGPT 모델 선택', 'ChatGPT model selector'))
        self.assert_selection('high', 'High')

    def test_missing_pro_does_not_mean_slider_max(self):
        self.load(slider_html(['Instant', 'Medium', 'High', 'Extra High']))
        self.assertFalse(review.select_model(self.page, 'pro')[0])

    def test_reordered_steps_follow_labels_not_fixed_positions(self):
        self.load(slider_html(['Instant', 'High', 'Medium', 'Extra High', 'Pro']))
        self.assert_selection('medium', 'Medium')

    def test_high_never_accepts_extra_high_only(self):
        self.load(slider_html(['Instant', 'Medium', 'Extra High', 'Pro']))
        self.assertFalse(review.select_model(self.page, 'high')[0])

    def test_old_slider_without_status_uses_verified_pill(self):
        self.load(slider_html(legacy=True).replace('role="status"', 'role="note"'))
        self.assert_selection('중간', 'Medium')

    def test_unknown_request_fails_closed(self):
        self.load(slider_html())
        self.assertFalse(review.select_model(self.page, 'turbo')[0])

    def test_stuck_slider_fails_closed(self):
        self.load(slider_html(stuck=True))
        self.assertFalse(review.select_model(self.page, 'pro')[0])

    def test_numeric_success_with_wrong_label_fails_closed(self):
        self.load(slider_html(stale=True))
        self.assertFalse(review.select_model(self.page, 'pro')[0])

    def test_hidden_latest_is_not_effort_or_current_model(self):
        self.load(slider_html(initial=4))
        self.page.locator('#trigger').click()
        state = review.read_menu_state(self.page)
        self.assertEqual(state['effort_checked'], 'Pro')
        self.assertNotIn('Latest', state['items'])
        self.assertNotIn('GPT-5.6 Sol', state['models'])
        self.assertNotEqual(state['model'], 'GPT-5.6 Sol')

    def test_effort_only_toggle_is_not_model_name(self):
        self.load(slider_html(initial=3))
        self.page.locator('#trigger').click()
        state = review.read_menu_state(self.page)
        self.assertIsNone(state['model'])
        self.assertEqual(state['effort_checked'], 'Extra High')

    def test_report_uses_final_model_not_stale_pro_toggle(self):
        self.load(slider_html(initial=4))
        ok, name = review.select_model(self.page, 'xhigh')
        self.assertTrue(ok)
        self.assertEqual(name, 'Unknown Model (Extra High)')

    def test_legacy_advanced_view_keeps_keyboard_slider_path(self):
        html = slider_html(legacy=True).replace(
            '<div role="status"', '<div role="menuitem" id="advanced">고급</div><div role="status"')
        html += '''<script>
 document.getElementById('advanced').onclick=()=>{
  document.getElementById('power').hidden=true;
  const radio=document.createElement('div');radio.setAttribute('role','menuitemradio');
  radio.textContent='Pro';menu.appendChild(radio);
 };
 trigger.onclick=()=>{menu.hidden=!menu.hidden;menu.dataset.state=menu.hidden?'closed':'open';
 document.getElementById('power').hidden=false;};
</script>'''
        self.load(html)
        self.assert_selection('pro', 'Pro')

    def test_model_pin_never_uses_hidden_alternative(self):
        self.load(slider_html())
        self.assertFalse(review.select_model(self.page, 'pro', require_model='GPT-5.6')[0])

    def test_mode_pressed_buttons_switch_to_chat(self):
        self.load('''<div role="group" aria-label="작성기 모드">
<button aria-pressed="false">Chat</button><button aria-pressed="true">Work</button></div>
<script>for(const b of document.querySelectorAll('button'))b.onclick=()=>{
for(const x of document.querySelectorAll('button'))x.setAttribute('aria-pressed',String(x===b));};</script>''')
        self.assertEqual(review.read_mode(self.page), 'Work')
        self.assertEqual(review.ensure_chat_mode(self.page), (True, 'Chat'))

    def test_failed_chat_transition_blocks_non_pro_effort(self):
        self.load(slider_html() + '<div role="group" aria-label="작성기 모드">'
                  '<button aria-pressed="false">Chat</button>'
                  '<button aria-pressed="true">Work</button></div>')
        self.assertFalse(review.select_model(self.page, 'high')[0])
        self.assertEqual(self.page.locator('#trigger').inner_text(), 'Instant')

    def test_legacy_mode_radios(self):
        self.load('<div role="radiogroup"><div role="radio" aria-checked="true">Chat</div></div>')
        self.assertEqual(review.ensure_chat_mode(self.page), (True, 'Chat'))

    def test_legacy_radio_aliases_are_exact(self):
        for request, expected in [('high', '높음'), ('중간', '중간'), ('pro', 'Pro'), ('xhigh', '매우 높음')]:
            with self.subTest(request=request):
                self.load(radio_html())
                ok, name = review.select_model(self.page, request)
                self.assertTrue(ok, name)
                self.assertEqual(self.page.locator('#trigger').inner_text(), expected)

    def test_legacy_slider(self):
        self.load(slider_html(legacy=True))
        self.assert_selection('extended', 'Extra High')

    def test_idless_composer_attachment_is_confirmed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp) / 'review-context.txt'
            path.write_text('local fixture only')
            self.load('''<div role="presentation"><div role="textbox" contenteditable="true"></div>
<input type="file"><span id="chip"></span></div><script>
document.querySelector('input').onchange=e=>document.getElementById('chip').textContent=e.target.files[0].name;
</script>''')
            self.assertTrue(review.attach_file(self.page, path))

    def test_attachment_chip_outside_presentation_but_inside_form(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp) / 'review-context.txt'
            path.write_text('local fixture only')
            self.load('''<form><div role="presentation"><div role="textbox" contenteditable="true"></div></div>
<input type="file"><span id="chip"></span></form><script>
document.querySelector('input').onchange=e=>document.getElementById('chip').textContent=e.target.files[0].name;
</script>''')
            self.assertTrue(review.attach_file(self.page, path))

    def test_attachment_name_outside_composer_is_not_confirmation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            path = Path(tmp) / 'review-context.txt'
            path.write_text('local fixture only')
            self.load('''<div>review-context.txt</div><div role="presentation">
<div role="textbox" contenteditable="true"></div><input type="file"></div>''')
            self.assertFalse(review.attach_file(self.page, path))


if __name__ == '__main__':
    unittest.main()
