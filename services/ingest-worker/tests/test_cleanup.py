import pytest

from pipeline.merge import cleanup


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Line one of the story.<br>Line two of the story.",
            "Line one of the story.\nLine two of the story.",
        ),
        (
            "First paragraph here.<br><br>\n<br>Second paragraph here.",
            "First paragraph here.\n\nSecond paragraph here.",
        ),
        ("A <i>very</i> <b>long</b> synopsis text.", "A very long synopsis text."),
        ("The synopsis of the show.<br><br>(Source: Crunchyroll)", "The synopsis of the show."),
        ("The synopsis of the show.\n\n(Written by MAL Rewrite)", "The synopsis of the show."),
        ("Tom &amp; Jerry go on an adventure.", "Tom & Jerry go on an adventure."),
        ("Too short.", None),
        ("", None),
        (None, None),
    ],
)
def test_anilist_description(raw, expected):
    assert cleanup.anilist_description(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Много лет назад [character=40882]титаны[/character] напали на людей.",
            "Много лет назад титаны напали на людей.",
        ),
        (
            "Сюжет продолжается здесь. [spoiler=спойлер]Эрен — титан.[/spoiler] Конец истории.",
            "Сюжет продолжается здесь. Конец истории.",
        ),
        (
            "Смотрите [anime=1]Ковбой Бибоп[/anime] и [url=https://x.y]ссылку[/url], "
            "[b]жирный[/b] текст.",
            "Смотрите Ковбой Бибоп и ссылку, жирный текст.",
        ),
        (
            "Первый абзац истории.\r\n\r\n\r\nВторой абзац истории.",
            "Первый абзац истории.\n\nВторой абзац истории.",
        ),
        ("[spoiler]всё скрыто[/spoiler]", None),
        (None, None),
    ],
)
def test_shikimori_description(raw, expected):
    assert cleanup.shikimori_description(raw) == expected


def test_clean_title_collapses_whitespace():
    assert cleanup.clean_title("  Attack   on\tTitan ") == "Attack on Titan"
    assert cleanup.clean_title("   ") is None
