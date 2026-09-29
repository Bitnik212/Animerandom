package picker.features

import io.kotest.matchers.collections.shouldContainExactly
import io.kotest.matchers.shouldBe
import io.ktor.client.request.get
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.support.ApiTest
import picker.support.CatalogStubs
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class RecommendationsTest : ApiTest() {
    private val id = UUID.randomUUID()

    private fun items(body: String): List<JsonObject> =
        body
            .json()
            .jsonObject["items"]!!
            .jsonArray
            .map { it.jsonObject }

    private fun JsonObject.at(vararg keys: String): String? =
        keys
            .fold(
                this as kotlinx.serialization.json.JsonElement?,
            ) { n, k -> (n as? JsonObject)?.get(k) }
            ?.jsonPrimitive
            ?.content

    @Test
    fun `engine results become localized cards with reason texts, in order`() =
        api { client ->
            CatalogStubs.recommendations(
                id,
                """
                [{"anime_id":8,"score":0.9,"reason":{"code":"similar_to","anime_id":3}},
                 {"anime_id":999,"score":0.8,"reason":{"code":"popular"}},
                 {"anime_id":12,"score":0.7,"reason":{"code":"liked_by_similar_users"}},
                 {"anime_id":9,"score":0.6,"reason":{"code":"popular_in_genre","genre":"drama"}},
                 {"anime_id":10,"score":0.5,"reason":{"code":"brand_new_code","extra":1}}]
                """.trimIndent(),
            )
            val response =
                client.get(
                    "/v1/users/me/recommendations?limit=5&lang=ru",
                ) { bearer(FakeKeycloak.token(sub = id)) }
            response.status shouldBe HttpStatusCode.OK
            val items = items(response.bodyAsText())
            items.map { it.at("card", "id") } shouldContainExactly listOf("8", "12", "9", "10")
            items[0].at("card", "title") shouldBe "Стальной алхимик: Братство"
            items[0].at("reason", "code") shouldBe "similar_to"
            items[0].at("reason", "text") shouldBe "Потому что вам понравилось «Врата Штейна»"
            items[0].at("reason", "animeId") shouldBe "3"
            items[1].at("reason", "text") shouldBe "Понравилось людям с похожим вкусом"
            items[2].at("reason", "text") shouldBe "Популярно в жанре «Драма»"
            items[2].at("reason", "genre") shouldBe "drama"
            items[3].at("reason", "code") shouldBe "popular" // unknown codes never leak
            items[3].at("reason", "text") shouldBe "Популярно сейчас"
        }

    @Test
    fun `when the engine fails, popular titles in liked genres fill the feed`() =
        api { client ->
            TestInfra.sql(
                "INSERT INTO app.app_user (id, liked_genres, disliked_genres) VALUES ('$id', '{sci-fi}', '{fantasy}')",
            )
            TestInfra.sql("INSERT INTO app.user_anime (user_id, anime_id, status) VALUES ('$id', 3, 'planned')")
            TestInfra.sql(
                "INSERT INTO app.user_feedback (user_id, anime_id, kind) VALUES ('$id', 11, 'not_interested')",
            )
            CatalogStubs.recommendations(id, "[]", status = 500)
            val items =
                items(
                    client
                        .get(
                            "/v1/users/me/recommendations?limit=4",
                        ) { bearer(FakeKeycloak.token(sub = id)) }
                        .bodyAsText(),
                )
            // Liked sci-fi first: 9 (3 is listed). Then overall by popularity, never fantasy (1, 4, 8, 10, 12),
            // listed (3), rejected (11), adult (6) or removed (7): 2, then 5.
            items.map { it.at("card", "id") } shouldContainExactly listOf("9", "2", "5")
            items.map { it.at("reason", "code") }.toSet() shouldBe setOf("popular")
        }

    @Test
    fun `a slow engine falls back too, and the feed needs a token`() =
        api { client ->
            CatalogStubs.recommendations(
                id,
                """[{"anime_id":1,"score":1,"reason":{"code":"popular"}}]""",
                delayMs = 1500,
            )
            val items =
                items(
                    client
                        .get(
                            "/v1/users/me/recommendations?limit=2",
                        ) { bearer(FakeKeycloak.token(sub = id)) }
                        .bodyAsText(),
                )
            items.map { it.at("card", "id") } shouldContainExactly listOf("8", "1")
            client.get("/v1/users/me/recommendations").status shouldBe HttpStatusCode.Unauthorized
            client.get("/v1/users/me/recommendations?limit=51") { bearer(FakeKeycloak.token(sub = id)) }.status shouldBe
                HttpStatusCode.BadRequest
        }
}
