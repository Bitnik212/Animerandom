from itertools import pairwise

import httpx
import pytest
import respx

from pipeline.http import ItemFailed, RateLimited, SourceStats, TokenBucket, TransientError
from pipeline.sources import anilist, annict, shikimori
from tests.conftest import fixture

ANILIST = "https://graphql.anilist.co"
SHIKI = "https://shikimori.io/api"


def test_token_bucket_is_shared_and_refills():
    bucket = TokenBucket("anilist", per_minute=60)  # capacity 2, 1 token/s
    assert bucket.try_take() == 0
    assert bucket.try_take() == 0
    wait = bucket.try_take()
    assert 0.5 < wait <= 1.0
    # A second worker sees the same bucket.
    assert TokenBucket("anilist", per_minute=60).try_take() > 0


def test_acquire_raises_when_wait_exceeds_inline_limit():
    bucket = TokenBucket("shikimori", per_minute=1)  # one token per minute
    bucket.acquire()
    with pytest.raises(RateLimited) as err:
        bucket.acquire(max_wait=0.1)
    assert err.value.wait > 50


@respx.mock
def test_requests_send_user_agent(settings):
    route = respx.post(ANILIST).mock(
        return_value=httpx.Response(200, json=fixture("anilist_page.json"))
    )
    with anilist.client() as c:
        anilist.fetch_page(c, anilist.PAGE_BY_POPULARITY_QUERY, {"page": 1})
    assert route.calls.last.request.headers["User-Agent"] == settings.HTTP_USER_AGENT


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, headers={"Retry-After": "17"}), RateLimited),
        (httpx.Response(502), TransientError),
        (httpx.Response(403, text="forbidden"), ItemFailed),
        (httpx.ConnectError("boom"), TransientError),
    ],
)
def test_errors_are_classified(response, error):
    respx.get(f"{SHIKI}/animes/1").mock(
        side_effect=[response] if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )
    with shikimori.client() as c, pytest.raises(error) as err:
        c.request("GET", "/animes/1")
    if error is RateLimited:
        assert err.value.wait == 17 and err.value.from_server


@respx.mock
def test_stats_count_requests_errors_and_throttles():
    respx.get(f"{SHIKI}/animes/1").mock(
        side_effect=[httpx.Response(200, json={}), httpx.Response(500), httpx.Response(429)]
    )
    with shikimori.client() as c:
        c.request("GET", "/animes/1")
        for _ in range(2):
            with pytest.raises((TransientError, RateLimited)):
                c.request("GET", "/animes/1")
    stats = SourceStats("shikimori").last_24h()
    assert (stats["requests"], stats["errors"], stats["throttled"]) == (3, 1, 1)
    assert stats["last_success"]


@respx.mock
def test_anilist_graphql_errors_map_to_item_or_transient():
    respx.post(ANILIST).mock(
        side_effect=[
            httpx.Response(
                200, json={"errors": [{"message": "Not Found.", "status": 404}], "data": None}
            ),
            httpx.Response(
                200, json={"errors": [{"message": "Internal", "status": 500}], "data": None}
            ),
        ]
    )
    with anilist.client() as c:
        with pytest.raises(ItemFailed):
            anilist.graphql(c, "query { x }", {})
        with pytest.raises(TransientError):
            anilist.graphql(c, "query { x }", {})


@respx.mock
def test_iter_id_range_pages_by_keyset():
    media = fixture("anilist_page.json")["data"]["Page"]["media"]
    first = {"data": {"Page": {"pageInfo": {"hasNextPage": True}, "media": media[:3]}}}
    second = {"data": {"Page": {"pageInfo": {"hasNextPage": False}, "media": media[3:]}}}
    route = respx.post(ANILIST).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    with anilist.client() as c:
        pages = list(anilist.iter_id_range(c, anilist.IdRange(0, 200_000)))
    assert [len(p) for p in pages] == [3, 3]
    import json

    variables = [json.loads(call.request.content)["variables"] for call in route.calls]
    assert variables[0]["idGreater"] == 0 and variables[0]["idLesser"] == 200_001
    assert variables[1]["idGreater"] == max(m["id"] for m in media[:3])


def test_id_ranges_cover_everything_with_an_open_end():
    ranges = anilist.id_ranges(5000)
    assert ranges[0] == anilist.IdRange(0, 5000)
    assert ranges[-1].end is None
    assert all(a.end == b.start for a, b in pairwise(ranges))


@respx.mock
def test_shikimori_404_means_no_entry():
    respx.get(f"{SHIKI}/animes/999").mock(return_value=httpx.Response(404))
    respx.get(f"{SHIKI}/animes/16498").mock(
        return_value=httpx.Response(200, json=fixture("shikimori_16498.json"))
    )
    with shikimori.client() as c:
        assert shikimori.fetch_by_mal_id(c, 999) is None
        assert shikimori.fetch_by_mal_id(c, 16498)["russian"] == "Атака титанов"


def test_shikimori_letter_prefixed_ids_are_text():
    assert shikimori.Anime.model_validate({"id": "z36098"}).id == "z36098"
    assert shikimori.Anime.model_validate({"id": 5114}).id == "5114"


@respx.mock
def test_annict_sends_token_and_parses_works(annict_token):
    route = respx.post("https://api.annict.com/graphql").mock(
        return_value=httpx.Response(200, json=fixture("annict_works.json"))
    )
    with annict.client() as c:
        works = annict.fetch_works(c, [1745])
    assert works[0]["title"] == "シュタインズ・ゲート"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"
