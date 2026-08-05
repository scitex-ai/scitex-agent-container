"""A question addressed to the OPERATOR must be seen, and must not be answered.

MEASURED 2026-08-03: scitex-app sat parked on an AskUserQuestion dialog
indefinitely. Every detector in prompts.py keys on "Enter to confirm" (16
occurrences); this dialog's footer says "Enter to select · ↑/↓ to navigate ·
n to add notes", a string that appeared NOWHERE in the module. No handler
matched, so the watchdog never saw it.

TWO PROPERTIES, and the second is the one that makes this safe:
  1. it is DETECTED (previously invisible)
  2. it is NOT ANSWERED -- the agent raised it because it needs the operator's
     judgement. In the observed case option 1 was a packaging decision that
     contradicted a standing operator ruling, so auto-selecting would have
     fabricated consent. The defect is that it hung SILENTLY, not that it hung.

The load-bearing test is therefore the negative one: zero keystrokes sent.
"Returns the handler name" would pass for a handler that answered it.

PA-307 / STX-TQ002 / STX-TQ007 -- one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container._runners._tmux.prompts import (
    _detect_operator_question,
    detect_and_respond,
)

# The real shape, from the operator's screenshot of scitex-app.
_ASK_USER_QUESTION = """
 PS-221 blocks every PR in scitex-app. How do you want it resolved?

 ❯ 1. Land the one-line fix now (Recommended)
   2. Finish #60 instead (PEP 735 groups)
   3. Config exemption with your directive as the reason

 Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel
"""

# A known auto-accept dialog, kept as the discriminator: it must NOT be
# mistaken for an operator question, or the watchdog would stop answering
# setup prompts it is supposed to answer.
#
# The option strings are taken VERBATIM from _detect_file_trust_radio's own
# required substrings, not invented. My first draft of this fixture said
# "1. Yes, proceed" and matched NOTHING -- so the test that was supposed to
# prove "auto-accept still works" passed zero keystrokes and failed, and it
# would have been just as easy to "fix" it by weakening the assertion. A
# fixture that cannot trigger the handler it names proves nothing about it.
_TRUST_RADIO = """
 Is this a project you created or one you trust?

   1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to exit
"""


def test_the_operator_question_dialog_is_detected():
    # Arrange — previously invisible: no detector contained "Enter to select".
    content = _ASK_USER_QUESTION
    # Act
    detected = _detect_operator_question(content)
    # Assert
    assert detected


def test_a_confirm_style_setup_dialog_is_not_an_operator_question():
    # Arrange — THE DISCRIMINATOR. If this matched, the new handler (priority 0)
    # would shadow the auto-accepters and the watchdog would stop answering
    # setup prompts entirely.
    content = _TRUST_RADIO
    # Act
    detected = _detect_operator_question(content)
    # Assert
    assert not detected


def test_no_keystrokes_are_sent_for_an_operator_question():
    # Arrange — THE LOAD-BEARING CASE. Answering would fabricate the operator's
    # consent; in the observed instance option 1 contradicted a standing ruling.
    sent: list[str] = []
    # Act
    detect_and_respond(_ASK_USER_QUESTION, set(), lambda k: sent.append(k))
    # Assert
    assert sent == []


def test_the_operator_question_is_reported_by_name():
    # Arrange — it must return a name rather than None, so the caller can
    # surface it and no lower-priority handler is reached.
    sent: list[str] = []
    # Act
    matched = detect_and_respond(_ASK_USER_QUESTION, set(), lambda k: sent.append(k))
    # Assert
    assert matched == "operator-question"


def test_a_normal_prompt_still_gets_its_keystrokes():
    # Arrange — the escalation path must not have broken auto-accept; a
    # detect-only handler is the exception, not the new rule.
    sent: list[str] = []
    # Act
    detect_and_respond(_TRUST_RADIO, set(), lambda k: sent.append(k))
    # Assert
    assert sent != []
