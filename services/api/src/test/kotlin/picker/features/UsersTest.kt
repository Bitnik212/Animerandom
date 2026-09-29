package picker.features

import com.github.tomakehurst.wiremock.client.WireMock.equalTo
import com.github.tomakehurst.wiremock.client.WireMock.exactly
import com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath
import com.github.tomakehurst.wiremock.client.WireMock.putRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.client.WireMock.urlPathMatching
import io.kotest.matchers.collections.shouldContainExactly
import io.kotest.matchers.collections.shouldContainExactlyInAnyOrder
import io.kotest.matchers.shouldBe
import io.ktor.client.HttpClient
import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.patch
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.coroutines.flow.toList
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
import picker.support.FakeKeycloak
import picker.support.KeycloakStubs
import picker.support.KeycloakStubs.ADMIN
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class UsersTest : ApiTest() {
    private val id = UUID.randomUUID()
    private val token get() =
        FakeKeycloak.token(
            sub = id,
            email = "me@example.com",
            roles = listOf("user", "offline_access"),
        )

    private suspend fun HttpClient.send(method: String, path: String, body: String? = null): HttpResponse {
        val build: io.ktor.client.request.HttpRequestBuilder.() -> Unit = {
            bearer(token)
            body?.let {
                contentType(ContentType.Application.Json)
                setBody(it)
            }
        }
        return when (method) {
            "GET" -> get(path, build)
            "PATCH" -> patch(path, build)
            "PUT" -> put(path, build)
            "POST" -> post(path, build)
            else -> delete(path, build)
        }
    }

    private suspend fun HttpResponse.obj(): JsonObject = bodyAsText().json().jsonObject

    private fun JsonObject.str(key: String) = this[key]?.takeUnless { it is JsonNull }?.jsonPrimitive?.content

    private fun excluded(): Set<String> =
        Redis(TestInfra.redisUrl).use { r ->
            runBlocking {
                r.commands
                    .smembers(Keys.userExcluded(id))
                    .toList()
                    .toSet()
            }
        }

    @Test
    fun `profile comes from the token and app_user, created on first use`() =
        api { client ->
            val body = client.send("GET", "/v1/users/me").obj()
            body.str("id") shouldBe id.toString()
            body.str("email") shouldBe "me@example.com"
            body["roles"].toString() shouldBe """["user"]"""
            body.str("locale") shouldBe "en"
            body.str("showAdult") shouldBe "false"
            body.str("onboardingCompleted") shouldBe "false"
            client.get("/v1/users/me").status shouldBe HttpStatusCode.Unauthorized
        }

    @Test
    fun `patch updates only the given fields and syncs a new locale to Keycloak first`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.user(id)
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id")
            client.send("PATCH", "/v1/users/me", """{"displayName":"  Neo  ","showAdult":true}""").obj().let {
                it.str("displayName") shouldBe "Neo"
                it.str("showAdult") shouldBe "true"
            }
            FakeKeycloak.server.verify(exactly(0), putRequestedFor(urlPathMatching("/admin/.*")))

            client.send("PATCH", "/v1/users/me", """{"locale":"ja-Latn"}""").obj().let {
                it.str("locale") shouldBe "ja-Latn"
                it.str("displayName") shouldBe "Neo"
            }
            FakeKeycloak.server.verify(
                putRequestedFor(
                    urlEqualTo("$ADMIN/users/$id"),
                ).withRequestBody(matchingJsonPath("$.attributes.locale[0]", equalTo("ja_latn"))),
            )
            client.send("PATCH", "/v1/users/me", """{"displayName":null}""").obj().str("displayName") shouldBe null
        }

    @Test
    fun `a Keycloak failure on locale change changes nothing`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.user(id)
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id", 500)
            client.send("PATCH", "/v1/users/me", """{"locale":"ru","showAdult":true}""").status shouldBe
                HttpStatusCode.BadGateway
            client.send("GET", "/v1/users/me").obj().let {
                it.str("locale") shouldBe "en"
                it.str("showAdult") shouldBe "false"
            }
        }

    @Test
    fun `patch validation`() =
        api { client ->
            listOf(
                """{"email":"x@y.z"}""" to "validation-failed",
                """{"showAdult":"yes"}""" to "validation-failed",
                """{"displayName":42}""" to "validation-failed",
                """{"displayName":"${"x".repeat(81)}"}""" to "validation-failed",
                """{"locale":"de"}""" to "unsupported-locale",
            ).forEach { (body, type) -> client.send("PATCH", "/v1/users/me", body).obj().str("type") shouldBe type }
        }

    @Test
    fun `onboarding stores favorites as completed 9 and the genre preferences`() =
        api { client ->
            client.send("GET", "/v1/anime/random?count=1") // builds user:{id}:excluded
            client
                .send(
                    "POST",
                    "/v1/users/me/onboarding",
                    """{"favorites":[1,3,3],"likedGenres":["action","Sci-Fi"],"dislikedGenres":["romance"]}""",
                ).status shouldBe HttpStatusCode.NoContent
            TestInfra.sql(
                "SELECT anime_id, status, score::int AS score FROM app.user_anime WHERE user_id = '$id' ORDER BY anime_id",
            ) shouldBe
                listOf(
                    mapOf("anime_id" to 1L, "status" to "completed", "score" to 9),
                    mapOf("anime_id" to 3L, "status" to "completed", "score" to 9),
                )
            client.send("GET", "/v1/users/me").obj().let {
                it.str("onboardingCompleted") shouldBe "true"
                it["likedGenres"].toString() shouldBe """["action","sci-fi"]"""
                it["dislikedGenres"].toString() shouldBe """["romance"]"""
            }
            excluded() shouldBe setOf("0", "1", "3")
        }

    @Test
    fun `onboarding validation`() =
        api { client ->
            client.send("POST", "/v1/users/me/onboarding", """{"favorites":[999]}""").obj().str("type") shouldBe
                "anime-not-found"
            client.send("POST", "/v1/users/me/onboarding", """{"likedGenres":["nope"]}""").obj().str("type") shouldBe
                "validation-failed"
            client
                .send("POST", "/v1/users/me/onboarding", """{"likedGenres":["action"],"dislikedGenres":["action"]}""")
                .obj()
                .str("type") shouldBe "validation-failed"
            client
                .send("POST", "/v1/users/me/onboarding", """{"favorites":[${(1..51).joinToString()}]}""")
                .obj()
                .str("type") shouldBe "validation-failed"
        }

    @Test
    fun `list entries keep the exclusion set in step`() =
        api { client ->
            client.send("GET", "/v1/anime/random") // builds the set
            client.send("PUT", "/v1/users/me/anime/8", """{"status":"planned"}""").obj().let {
                it.str("userStatus") shouldBe "planned"
                it.str("userScore") shouldBe null
                it.containsKey("synopsis") shouldBe false
            }
            excluded() shouldBe setOf("0")
            client
                .send(
                    "PUT",
                    "/v1/users/me/anime/8",
                    """{"status":"completed","score":10}""",
                ).obj()
                .str("userScore") shouldBe
                "10"
            excluded() shouldBe setOf("0", "8")

            client.send("POST", "/v1/users/me/feedback", """{"animeId":8,"kind":"not_interested"}""").status shouldBe
                HttpStatusCode.NoContent
            client.send("PUT", "/v1/users/me/anime/8", """{"status":"planned"}""")
            excluded() shouldBe setOf("0", "8") // still rejected, so still excluded
            client.send("DELETE", "/v1/users/me/anime/8").status shouldBe HttpStatusCode.NoContent
            excluded() shouldBe setOf("0", "8")

            client.send("PUT", "/v1/users/me/anime/9", """{"status":"dropped"}""")
            client.send("DELETE", "/v1/users/me/anime/9")
            excluded() shouldBe setOf("0", "8")
            client.send("POST", "/v1/users/me/feedback", """{"animeId":10,"kind":"skipped"}""")
            excluded() shouldBe setOf("0", "8")
        }

    @Test
    fun `a planned title can't carry a score`() =
        api { client ->
            client.send("PUT", "/v1/users/me/anime/1", """{"status":"planned","score":8}""").obj().str("type") shouldBe
                "validation-failed"
            client.send("PUT", "/v1/users/me/anime/1", """{"status":"planned"}""").status shouldBe HttpStatusCode.OK
            client.send("PUT", "/v1/users/me/anime/1", """{"status":"dropped","score":3}""").status shouldBe
                HttpStatusCode.OK
        }

    @Test
    fun `list entry and feedback validation`() =
        api { client ->
            client.send("PUT", "/v1/users/me/anime/1", """{"status":"loved"}""").obj().str("type") shouldBe
                "validation-failed"
            client
                .send(
                    "PUT",
                    "/v1/users/me/anime/1",
                    """{"status":"completed","score":11}""",
                ).obj()
                .str("type") shouldBe
                "validation-failed"
            client.send("PUT", "/v1/users/me/anime/999", """{"status":"planned"}""").obj().str("type") shouldBe
                "anime-not-found"
            client.send("PUT", "/v1/users/me/anime/x", """{"status":"planned"}""").obj().str("type") shouldBe
                "validation-failed"
            client.send("POST", "/v1/users/me/feedback", """{"animeId":1,"kind":"meh"}""").obj().str("type") shouldBe
                "validation-failed"
            client
                .send(
                    "POST",
                    "/v1/users/me/feedback",
                    """{"animeId":999,"kind":"skipped"}""",
                ).obj()
                .str("type") shouldBe
                "anime-not-found"
        }

    @Test
    fun `the list pages, filters by status and sorts`() =
        api { client ->
            client.send("GET", "/v1/users/me")
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status, score, created_at) VALUES " +
                    "('$id', 11, 'watching', 6, now() - interval '3 days'), " +
                    "('$id', 12, 'completed', 10, now() - interval '2 days'), " +
                    "('$id', 3, 'completed', NULL, now() - interval '1 day'), " +
                    "('$id', 5, 'planned', NULL, now())",
            )

            fun ids(body: JsonObject) = body["items"]!!.jsonArray.map { it.jsonObject.str("id")!!.toLong() }
            ids(client.send("GET", "/v1/users/me/anime").obj()) shouldContainExactly listOf(5L, 3L, 12L, 11L)
            // unrated last; equal timestamps fall back to the anime ID
            ids(client.send("GET", "/v1/users/me/anime?sort=score").obj()) shouldContainExactly listOf(12L, 11L, 3L, 5L)
            ids(client.send("GET", "/v1/users/me/anime?sort=length").obj()) shouldContainExactly
                listOf(12L, 3L, 5L, 11L)
            client.send("GET", "/v1/users/me/anime?status=completed&size=1&page=2").obj().let {
                ids(it) shouldContainExactly listOf(12L)
                it.str("total") shouldBe "2"
            }
            client
                .send("GET", "/v1/users/me/anime?status=completed")
                .obj()["items"]!!
                .jsonArray
                .map { it.jsonObject.str("userStatus") }
                .toSet() shouldBe setOf("completed")
            client.send("GET", "/v1/users/me/anime?sort=title").obj().str("type") shouldBe "validation-failed"
            client.send("GET", "/v1/users/me/anime?status=x").obj().str("type") shouldBe "validation-failed"
            ids(client.send("GET", "/v1/users/me/anime?status=dropped").obj()) shouldContainExactlyInAnyOrder
                emptyList()
        }
}
