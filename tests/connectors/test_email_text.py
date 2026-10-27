import pytest

from ai_oil.connectors.email_text import (
    html_to_text,
    new_text,
    normalise_body,
    strip_quoted,
    strip_signature,
    subject_root,
)

NEW_PART = "Hi Jane,\n\nthe inductor lead time is confirmed at 12 weeks.\n\nBest regards\nAlex"

ENGLISH_OUTLOOK = """Hi Jane,

the inductor lead time is confirmed at 12 weeks.

Best regards
Alex

From: Jane Doe <jane@acme.com>
Sent: Monday, October 26, 2026 9:15 AM
To: Alex Hultsch <alex@visteon.example>
Cc: Bob <bob@acme.com>
Subject: Inductor lead time

Alex, what is the lead time for the inductor?
"""

GERMAN_OUTLOOK = """Hi Jane,

the inductor lead time is confirmed at 12 weeks.

Best regards
Alex

Von: Jane Doe <jane@acme.com>
Gesendet: Montag, 26. Oktober 2026 09:15
An: Alex Hultsch <alex@visteon.example>
Betreff: AW: Drossel Lieferzeit

Alex, wie ist die Lieferzeit?
"""


def test_english_outlook_reply() -> None:
    assert strip_quoted(ENGLISH_OUTLOOK) == NEW_PART


def test_german_outlook_reply() -> None:
    assert strip_quoted(GERMAN_OUTLOOK) == NEW_PART


def test_outlook_block_with_bold_markers() -> None:
    text = (
        "Confirmed.\n\n"
        "*From:* Jane Doe <jane@acme.com>\n"
        "*Sent:* Monday, October 26, 2026 9:15 AM\n"
        "*To:* Alex <alex@visteon.example>\n"
        "*Subject:* Inductor lead time\n\n"
        "Older text"
    )
    assert strip_quoted(text) == "Confirmed."


def test_outlook_header_matching_is_case_insensitive_and_quote_tolerant() -> None:
    text = "Done.\n> FROM: Jane\n> SENT: yesterday\n> TO: Alex\n> old"
    assert strip_quoted(text) == "Done."


@pytest.mark.parametrize(
    "separator",
    [
        "-----Original Message-----",
        "-----Ursprüngliche Nachricht-----",
        "---------- Forwarded message ---------",
        "---------- Weitergeleitete Nachricht ----------",
        "----- original message -----",
    ],
)
def test_separator_lines(separator: str) -> None:
    text = f"Thanks, see below.\n\n{separator}\nFrom: someone\nold body"
    assert strip_quoted(text) == "Thanks, see below."


def test_on_wrote_single_line() -> None:
    text = "Agreed.\n\nOn Mon, 26 Oct 2026 at 09:15, Jane <jane@acme.com> wrote:\n\nOld text"
    assert strip_quoted(text) == "Agreed."


def test_on_wrote_wrapped_over_two_lines() -> None:
    text = "Agreed.\n\nOn Mon, 26 Oct 2026 at 09:15, Jane Doe <jane.doe@acme.com>\nwrote:\n\nOld text"
    assert strip_quoted(text) == "Agreed."


def test_german_schrieb_single_line() -> None:
    text = "Einverstanden.\n\nAm Mo., 26. Okt. 2026 um 09:15 Uhr schrieb Jane <jane@acme.com>:\n\nAlter Text"
    assert strip_quoted(text) == "Einverstanden."


def test_german_schrieb_wrapped_over_two_lines() -> None:
    text = "Einverstanden.\n\nAm Mo., 26. Okt. 2026 um 09:15 Uhr schrieb Jane\n<jane@acme.com>:\n\nAlter Text"
    assert strip_quoted(text) == "Einverstanden."


def test_underscore_separator_before_von_block() -> None:
    text = "Gerne.\n\n________________________________\nVon: Jane <jane@acme.com>\nAlter Text ohne Kopfzeilen"
    assert strip_quoted(text) == "Gerne."


def test_underscore_line_without_from_does_not_cut() -> None:
    text = "Part one\n____________\nPart two"
    assert strip_quoted(text) == text


def test_gmail_quote_block() -> None:
    text = "Sounds good.\n\n> earlier line\n> another line\n>\n> last line\n"
    assert strip_quoted(text) == "Sounds good."


def test_inline_quote_followed_by_new_text_does_not_cut() -> None:
    text = "> you asked about the lead time\n12 weeks.\n\n> and the price\nEUR 0.42 per piece."
    assert strip_quoted(text) == text


def test_from_in_normal_prose_does_not_cut() -> None:
    text = "From: my point of view the schedule holds.\nSent: the samples on Friday.\nNo more headers."
    assert strip_quoted(text) == text


def test_from_without_recipient_line_does_not_cut() -> None:
    text = "From: Jane\nSent: Monday\nthat is all\nthere is\nto say\nand more\nTo: you"
    assert strip_quoted(text) == text


def test_first_marker_wins() -> None:
    text = "New.\n\nOn Mon, 26 Oct 2026, Jane wrote:\n> x\n\n-----Original Message-----\nFrom: x"
    assert strip_quoted(text) == "New."


def test_no_marker_keeps_text_unchanged() -> None:
    text = "Just a plain message.\n\nSecond paragraph.\n"
    assert strip_quoted(text) == text


def test_marker_on_first_line_leaves_empty_text() -> None:
    assert strip_quoted("-----Original Message-----\nFrom: x") == ""


def test_signature_delimiter() -> None:
    assert strip_signature("Hello\n\n-- \nAlex\nProgram Lead") == "Hello"
    assert strip_signature("Hello\n--\nAlex") == "Hello"


def test_double_dash_inside_text_is_not_a_delimiter() -> None:
    text = "Hello -- really\nbye"
    assert strip_signature(text) == text


def test_sent_from_my_device() -> None:
    assert strip_signature("Will call you.\n\nSent from my iPhone") == "Will call you."


def test_german_device_signatures() -> None:
    assert strip_signature("Rufe an.\n\nVon meinem iPhone gesendet") == "Rufe an."
    assert strip_signature("Rufe an.\n\nGesendet von meinem iPad") == "Rufe an."


def test_von_meinem_without_gesendet_is_not_a_signature() -> None:
    text = "Von meinem Standpunkt aus passt der Termin."
    assert strip_signature(text) == text


def test_extra_markers() -> None:
    text = "Done.\n\nMit freundlichen Grüßen\nAlex"
    assert strip_signature(text, ["Mit freundlichen"]) == "Done."
    assert strip_signature(text) == text
    assert strip_signature(text, [""]) == text


def test_extra_markers_are_case_sensitive() -> None:
    text = "Done.\n\nbest regards\nAlex"
    assert strip_signature(text, ["Best regards"]) == text


def test_new_text_of_reply() -> None:
    assert new_text(ENGLISH_OUTLOOK) == NEW_PART


def test_new_text_removes_quote_and_signature() -> None:
    text = "Okay.\r\n\r\n-- \r\nAlex\r\n\r\nOn Mon, 26 Oct 2026, Jane wrote:\r\n> hi"
    assert new_text(text) == "Okay."


def test_new_text_of_pure_forward_is_none() -> None:
    text = "---------- Forwarded message ---------\nFrom: Jane\nSent: Monday\nTo: Alex\n\nBody"
    assert new_text(text) is None


def test_new_text_of_blank_text_is_none() -> None:
    assert new_text(" \n \n") is None


def test_new_text_without_markers_is_normalised_text() -> None:
    assert new_text("Line  one here  \r\n\r\n\r\n\r\nLine two\r\n") == "Line one here\n\nLine two"


def test_new_text_extra_markers() -> None:
    assert new_text("Done.\nMit freundlichen Grüßen\nAlex", extra_markers=["Mit freundlichen"]) == "Done."


def test_normalise_line_endings() -> None:
    assert normalise_body("a\r\nb\rc") == "a\nb\nc"


def test_normalise_spaces() -> None:
    assert normalise_body("a b c　d") == "a b c d"


def test_normalise_zero_width() -> None:
    assert normalise_body("﻿ab​c‌d‍e") == "abcde"


def test_normalise_blank_lines() -> None:
    assert normalise_body("\n\n\na\n\n\n\nb\n \n\t\nc\n\n\n") == "a\n\nb\n\nc"


def test_normalise_trailing_whitespace_and_space_runs() -> None:
    assert normalise_body("a   b \t \nc\t\nd") == "a b\nc\nd"


def test_normalise_keeps_tabs_between_cells() -> None:
    assert normalise_body("A\tB\t\tC") == "A\tB\t\tC"


def test_html_paragraphs() -> None:
    assert html_to_text("<p>One</p><p>Two</p>") == "One\nTwo"


def test_html_line_breaks() -> None:
    assert html_to_text("<div>Hello<br>Jane<br/>Bye</div>") == "Hello\nJane\nBye"


def test_html_table_rows_use_tabs() -> None:
    html = (
        "<table><tr><td>Part</td><td> Qty </td><th>EUR</th></tr>\n"
        "<tr><td>L1</td><td>10</td><td>0.42</td></tr></table>"
    )
    assert html_to_text(html) == "Part\tQty\tEUR\nL1\t10\t0.42"


def test_html_drops_script_style_head() -> None:
    html = (
        "<html><head><title>T</title><style>p {color: red}</style></head>"
        "<body><script>alert(1)</script><p>Visible</p><style>x{}</style></body></html>"
    )
    assert html_to_text(html) == "Visible"


def test_html_nbsp_entity() -> None:
    assert html_to_text("<p>a&nbsp;&nbsp;b&nbsp;</p>") == "a b"


def test_html_nested_divs_give_at_most_one_blank_line() -> None:
    html = "<div><div><div>One</div></div><div><div><p>Two</p></div></div></div><div>Three</div>"
    text = html_to_text(html)
    assert "\n\n\n" not in text
    assert text.split() == ["One", "Two", "Three"]
    assert text.startswith("One")


def test_html_source_formatting_does_not_create_lines() -> None:
    html = "<div>\n  Hello\n  world\n</div>\n<div>\n  Next\n</div>"
    assert html_to_text(html) == "Hello world\nNext"


def test_html_headings_lists_and_blockquote() -> None:
    html = "<h1>Title</h1><ul><li>One</li><li>Two</li></ul><blockquote>Quoted</blockquote>"
    assert html_to_text(html) == "Title\nOne\nTwo\nQuoted"


def test_html_outlook_reply_end_to_end() -> None:
    html = (
        "<html><body><div>Confirmed.</div><div><br></div>"
        "<div><b>From:</b> Jane Doe<br><b>Sent:</b> Monday<br><b>To:</b> Alex<br>"
        "<b>Subject:</b> Lead time</div><div>Old text</div></body></html>"
    )
    assert new_text(html_to_text(html)) == "Confirmed."


def test_html_comment_is_ignored() -> None:
    assert html_to_text("<p>a<!-- hidden -->b</p>") == "ab"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("RE: AW: WG: Inductor lead time", "Inductor lead time"),
        ("Re[2]: x", "x"),
        ("AW[3]: RE: x", "x"),
        ("[EXTERNAL] FW: x", "x"),
        ("[EXT] RE: [EXTERNAL] x", "x"),
        ("Antw: x", "x"),
        ("Antwort: x", "x"),
        ("FWD: x", "x"),
        ("Fwd:x", "x"),
        ("re:re:  x", "x"),
        ("Inductor lead time", "Inductor lead time"),
        ("  Inductor   lead \t time  ", "Inductor lead time"),
        ("Regarding the RE: tag", "Regarding the RE: tag"),
        ("Reminder: x", "Reminder: x"),
        ("Re: ", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_subject_root(subject: str | None, expected: str) -> None:
    assert subject_root(subject) == expected
