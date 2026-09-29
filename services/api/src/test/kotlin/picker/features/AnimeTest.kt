package picker.features

import com.github.tomakehurst.wiremock.client.WireMock.equalTo
import com.github.tomakehurst.wiremock.client.WireMock.exactly
import com.github.tomakehurst.wiremock.client.WireMock.getRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlPathMatching
import io.kotest.matchers.collections.shouldContainExactly
import io.kotest.matchers.longs.shouldBeInRange
import io.kotest.matchers.shouldBe
import io.ktor.client.request.get
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import picker.support.ApiTest
import picker.support.CatalogStubs
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class AnimeTest : ApiTest() {
    private suspend fun HttpResponse.obj(): JsonObject = bodyAsText().json().jsonObject

    private fun JsonObject.str(key: String) = this[key]?.takeUnless { it is JsonNull }?.jsonPrimitive?.content

    private fun ids(body: JsonObject) =
        body["items"]!!.jsonArray.map {
            it.jsonObject["id"]!!
                .jsonPrimitive.content
                .toLong()
        }

    @Test
    fun `details are localized, with tags, studios and relations`() =
        api { client ->
            val response = client.get("/v1/anime/1?lang=ru")
            response.status shouldBe HttpStatusCode.OK
            response.headers[HttpHeaders.ContentLanguage] shouldBe "ru"
            val body = response.obj()
            body.str("title") shouldBe "Атака титанов"
            body.str("titleLocale") shouldBe "ru"
            body.str("titleNative") shouldBe "進撃の巨人"
            body.str("titleRomaji") shouldBe "Shingeki no Kyojin"
            body.str("synopsis") shouldBe "Человечество сражается с титанами."
            body.str("synopsisMachine") shouldBe "false"
            body.str("score") shouldBe "8.5"
            body["genres"]!!.jsonArray.map { it.jsonObject.str("name") } shouldContainExactly
                listOf("Экшен", "Драма", "Фэнтези")
            body["tags"]!!.jsonArray.map {
                it.jsonObject.str(
                    "name",
                ) to it.jsonObject.str("spoiler")
            } shouldContainExactly
                listOf("Военное" to "false", "Трагедия" to "true")
            body["studios"]!!.jsonArray.map { it.jsonObject.str("name") } shouldContainExactly listOf("Studio One")
            val relation = body["relations"]!!.jsonArray.single().jsonObject
            relation.str("kind") shouldBe "SEQUEL"
            val sequel = relation["anime"]!!.jsonObject
            sequel.str("id") shouldBe "2"
            sequel.containsKey("synopsis") shouldBe false // list cards have no synopsis
            body.str("userStatus") shouldBe null
        }

    @Test
    fun `missing translations fall back along the chain and say which locale was used`() =
        api { client ->
            client.get("/v1/anime/2?lang=ru").obj().let {
                it.str("title") shouldBe "Attack on Titan Season 2"
                it.str("titleLocale") shouldBe "en"
                it.str("synopsisLocale") shouldBe "en"
            }
            client.get("/v1/anime/4?lang=ru").obj().let {
                it.str("title") shouldBe "Kyoukai no Kanata"
                it.str("titleLocale") shouldBe "ja-Latn"
                it.str("synopsis") shouldBe null
                it.str("synopsisLocale") shouldBe null
            }
            client.get("/v1/anime/5?lang=en").obj().let {
                it.str("title") shouldBe "ぼくらの日常"
                it.str("titleLocale") shouldBe "ja"
                it.str("synopsis") shouldBe null // English synopses don't fall back to Japanese
            }
            client.get("/v1/anime/5?lang=ja").obj().str("synopsis") shouldBe "海辺の町の日常。"
        }

    @Test
    fun `unknown and malformed ids`() =
        api { client ->
            client.get("/v1/anime/999").let {
                it.status shouldBe HttpStatusCode.NotFound
                it.obj().str("type") shouldBe "anime-not-found"
            }
            client.get("/v1/anime/abc").obj().str("type") shouldBe "validation-failed"
        }

    @Test
    fun `cards are cached per locale for an hour`() =
        api { client ->
            client.get("/v1/anime/3?lang=en").obj().str("title") shouldBe "Steins;Gate"
            TestInfra.sql(
                "UPDATE catalog.anime_localization SET title = 'Changed' WHERE anime_id = 3 AND locale = 'en'",
            )
            client.get("/v1/anime/3?lang=en").obj().str("title") shouldBe "Steins;Gate"
            client.get("/v1/anime/3?lang=ja-Latn").obj().str("title") shouldBe "Steins;Gate"
            Redis(TestInfra.redisUrl).use { redis ->
                runBlocking { redis.commands.ttl(Keys.animeCard(3, "en")) }!! shouldBeInRange 3500L..3600L
            }
            TestInfra.sql(
                "UPDATE catalog.anime_localization SET title = 'Steins;Gate' WHERE anime_id = 3 AND locale = 'en'",
            )
        }

    @Test
    fun `signed-in users see their own status and score`() =
        api { client ->
            val user = UUID.randomUUID()
            TestInfra.sql("INSERT INTO app.app_user (id) VALUES ('$user')")
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status, score) VALUES ('$user', 2, 'completed', 9)",
            )
            val body = client.get("/v1/anime/1") { bearer(FakeKeycloak.token(sub = user)) }.obj()
            body.str("userStatus") shouldBe null
            val sequel =
                body["relations"]!!
                    .jsonArray
                    .single()
                    .jsonObject["anime"]!!
                    .jsonObject
            sequel.str("userStatus") shouldBe "completed"
            sequel.str("userScore") shouldBe "9"
        }

    @Test
    fun `similar keeps the engine's order and hides adult titles unless opted in`() =
        api { client ->
            CatalogStubs.similar(1, listOf(8, 999, 10, 3))
            ids(client.get("/v1/anime/1/similar?limit=5").obj()) shouldContainExactly listOf(8L, 10L, 3L)
            FakeKeycloak.server.verify(
                getRequestedFor(urlPathMatching("/rec/v1/similar/1"))
                    .withQueryParam("limit", equalTo("5"))
                    .withoutQueryParam("includeAdult"),
            )

            val adult = UUID.randomUUID()
            TestInfra.sql("INSERT INTO app.app_user (id, show_adult) VALUES ('$adult', true)")
            client.get("/v1/anime/1/similar") { bearer(FakeKeycloak.token(sub = adult)) }
            FakeKeycloak.server.verify(
                getRequestedFor(urlPathMatching("/rec/v1/similar/1")).withQueryParam("includeAdult", equalTo("true")),
            )
        }

    @Test
    fun `similar degrades to an empty list when the engine fails or is slow`() =
        api { client ->
            CatalogStubs.similar(1, emptyList(), status = 500)
            client.get("/v1/anime/1/similar").let {
                it.status shouldBe HttpStatusCode.OK
                ids(it.obj()) shouldBe emptyList()
            }
            CatalogStubs.similar(1, listOf(2), delayMs = 1500)
            ids(client.get("/v1/anime/1/similar").obj()) shouldBe emptyList()
        }

    @Test
    fun `similar for an unknown anime is 404 without asking the engine`() =
        api { client ->
            client.get("/v1/anime/999/similar").obj().str("type") shouldBe "anime-not-found"
            FakeKeycloak.server.verify(exactly(0), getRequestedFor(urlPathMatching("/rec/.*")))
            client.get("/v1/anime/1/similar?limit=0").obj().str("type") shouldBe "validation-failed"
        }
}
