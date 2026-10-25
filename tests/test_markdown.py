import pytest
from claudia_telegram.markdown import to_telegram_html


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold** and __also__", "<b>bold</b> and <b>also</b>"),
        ("*italic* text", "<i>italic</i> text"),
        ("`a < b`", "<code>a &lt; b</code>"),
        ("# Title", "<b>Title</b>"),
        ("- one\n- two", "• one\n• two"),
        ("[site](https://x.test/?a=1&b=2)", '<a href="https://x.test/?a=1&amp;b=2">site</a>'),
    ],
)
def test_supported_markdown(source: str, expected: str) -> None:
    assert to_telegram_html(source) == expected


def test_code_block_contents_are_not_formatted() -> None:
    result = to_telegram_html("```\n**not bold** <tag>\n```")
    assert result == "<pre>**not bold** &lt;tag&gt;</pre>"


def test_plain_text_is_escaped() -> None:
    assert to_telegram_html("1 < 2 & 3") == "1 &lt; 2 &amp; 3"
