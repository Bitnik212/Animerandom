package picker.features.auth

import com.github.tomakehurst.wiremock.client.WireMock.containing
import com.github.tomakehurst.wiremock.client.WireMock.deleteRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.equalTo
import com.github.tomakehurst.wiremock.client.WireMock.equalToJson
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath
import com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.putRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import io.kotest.matchers.longs.shouldBeInRange
import io.kotest.matchers.shouldBe
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.auth.KeycloakClient
import picker.i18n.AppLocale
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import picker.support.ApiTest
import picker.support.FakeKeycloak
import picker.support.KeycloakStubs
import picker.support.KeycloakStubs.ADMIN
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import picker.support.testConfig
import java.util.UUID

class AccountTest : ApiTest() {
    private val kc get() = FakeKeycloak.server
    private val id = UUID.randomUUID()

    @Test
    fun `password change verifies the current password, then resets`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.passwordGrant()
            KeycloakStubs.logout()
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id/reset-password")
            val response =
                client.put("/v1/users/me/password") {
                    bearer(FakeKeycloak.token(sub = id, email = "me@example.com"))
                    contentType(ContentType.Application.Json)
                    setBody("""{"currentPassword":"old-password","newPassword":"new-password-1"}""")
                }
            response.status shouldBe HttpStatusCode.NoContent
            kc.verify(
                postRequestedFor(
                    urlEqualTo(KeycloakStubs.TOKEN),
                ).withRequestBody(containing("username=me%40example.com")),
            )
            // The verification grant's session is ended right away.
            kc.verify(
                postRequestedFor(
                    urlEqualTo(KeycloakStubs.LOGOUT),
                ).withRequestBody(containing("refresh_token=refresh-token")),
            )
            kc.verify(
                putRequestedFor(urlEqualTo("$ADMIN/users/$id/reset-password"))
                    .withRequestBody(equalToJson("""{"type":"password","value":"new-password-1","temporary":false}""")),
            )
        }

    @Test
    fun `a wrong current password is invalid-credentials`() =
        api { client ->
            KeycloakStubs.invalidGrant("Invalid user credentials")
            val response =
                client.put("/v1/users/me/password") {
                    bearer(FakeKeycloak.token(sub = id))
                    contentType(ContentType.Application.Json)
                    setBody("""{"currentPassword":"nope-nope","newPassword":"new-password-1"}""")
                }
            response
                .bodyAsText()
                .json()
                .jsonObject["type"]!!
                .jsonPrimitive.content shouldBe "invalid-credentials"
        }

    @Test
    fun `logout everywhere ends every session`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.ok("POST", "$ADMIN/users/$id/logout")
            client.post("/v1/users/me/logout-all") { bearer(FakeKeycloak.token(sub = id)) }.status shouldBe
                HttpStatusCode.NoContent
            kc.verify(postRequestedFor(urlEqualTo("$ADMIN/users/$id/logout")))
        }

    @Test
    fun `account deletion removes app rows, user keys, and the Keycloak user`() =
        api { client ->
            TestInfra.sql("INSERT INTO app.app_user (id) VALUES ('$id')")
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status, score) VALUES ('$id', 1, 'completed', 9)",
            )
            TestInfra.sql("INSERT INTO app.user_feedback (user_id, anime_id, kind) VALUES ('$id', 3, 'not_interested')")
            Redis(TestInfra.redisUrl).use { runBlocking { it.commands.sadd(Keys.userExcluded(id), "1", "3") } }
            KeycloakStubs.serviceAccount()
            KeycloakStubs.ok("DELETE", "$ADMIN/users/$id")

            client.delete("/v1/users/me") { bearer(FakeKeycloak.token(sub = id)) }.status shouldBe
                HttpStatusCode.NoContent
            TestInfra.sql("SELECT count(*) AS n FROM app.app_user WHERE id = '$id'") shouldBe listOf(mapOf("n" to 0L))
            TestInfra.sql("SELECT count(*) AS n FROM app.user_anime WHERE user_id = '$id'") shouldBe
                listOf(mapOf("n" to 0L))
            Redis(TestInfra.redisUrl).use { runBlocking { it.commands.exists(Keys.userExcluded(id)) } } shouldBe 0L
            kc.verify(deleteRequestedFor(urlEqualTo("$ADMIN/users/$id")))

            // The access token is still valid for minutes, but it can't bring the account back.
            val leftover = client.get("/v1/meta/genres") { bearer(FakeKeycloak.token(sub = id)) }
            leftover.status shouldBe HttpStatusCode.Unauthorized
            TestInfra.sql("SELECT count(*) AS n FROM app.app_user WHERE id = '$id'") shouldBe listOf(mapOf("n" to 0L))
            Redis(TestInfra.redisUrl).use { runBlocking { it.commands.ttl(Keys.deletedUser(id)) } }!! shouldBeInRange
                3500L..3600L
            client.get("/v1/meta/genres").status shouldBe HttpStatusCode.OK // anonymous use still works
        }

    @Test
    fun `deletion is retryable - Keycloak failing is 502, a gone user is fine`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.ok("DELETE", "$ADMIN/users/$id", 500)
            client.delete("/v1/users/me") { bearer(FakeKeycloak.token(sub = id)) }.status shouldBe
                HttpStatusCode.BadGateway
            KeycloakStubs.ok("DELETE", "$ADMIN/users/$id", 404)
            client.delete("/v1/users/me") { bearer(FakeKeycloak.token(sub = id)) }.status shouldBe
                HttpStatusCode.NoContent
        }

    @Test
    fun `admins can view users and grant or revoke the admin role`() =
        api { client ->
            TestInfra.sql("INSERT INTO app.app_user (id) VALUES ('$id')")
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status, score) " +
                    "VALUES ('$id', 1, 'completed', 9), ('$id', 3, 'planned', NULL)",
            )
            KeycloakStubs.serviceAccount()
            KeycloakStubs.user(id)
            kc.stubFor(
                get(urlEqualTo("$ADMIN/users/$id/role-mappings/realm/composite"))
                    .willReturn(
                        KeycloakStubs.json(
                            200,
                            """[{"name":"user"},{"name":"default-roles-anime-picker"},{"name":"offline_access"}]""",
                        ),
                    ),
            )
            val adminRole = """{"id":"r1","name":"admin"}"""
            kc.stubFor(
                get(urlEqualTo("$ADMIN/users/$id/role-mappings/realm/available"))
                    .willReturn(KeycloakStubs.json(200, """[{"id":"r2","name":"offline_access"},$adminRole]""")),
            )
            kc.stubFor(
                get(urlEqualTo("$ADMIN/users/$id/role-mappings/realm"))
                    .willReturn(
                        KeycloakStubs.json(200, """[{"id":"r3","name":"default-roles-anime-picker"},$adminRole]"""),
                    ),
            )
            KeycloakStubs.ok("POST", "$ADMIN/users/$id/role-mappings/realm")
            KeycloakStubs.ok("DELETE", "$ADMIN/users/$id/role-mappings/realm")
            val admin = FakeKeycloak.token(roles = listOf("user", "admin"))

            val view =
                client
                    .get("/v1/admin/users/$id") { bearer(admin) }
                    .bodyAsText()
                    .json()
                    .jsonObject
            view["roles"].toString() shouldBe """["user"]"""
            view["listSize"]!!.jsonPrimitive.content shouldBe "2"
            view["ratings"]!!.jsonPrimitive.content shouldBe "1"

            client.put("/v1/admin/users/$id/admin") { bearer(admin) }.status shouldBe HttpStatusCode.NoContent
            kc.verify(
                postRequestedFor(urlEqualTo("$ADMIN/users/$id/role-mappings/realm"))
                    .withRequestBody(equalToJson("""[{"id":"r1","name":"admin"}]""")),
            )
            client.delete("/v1/admin/users/$id/admin") { bearer(admin) }.status shouldBe HttpStatusCode.NoContent
            kc.verify(
                deleteRequestedFor(urlEqualTo("$ADMIN/users/$id/role-mappings/realm"))
                    .withRequestBody(equalToJson("""[{"id":"r1","name":"admin"}]""")),
            )
            client.put("/v1/admin/users/$id/admin") { bearer(FakeKeycloak.token()) }.status shouldBe
                HttpStatusCode.Forbidden
        }

    @Test
    fun `locale sync keeps every other attribute`() =
        runBlocking {
            KeycloakStubs.serviceAccount()
            KeycloakStubs.user(id, attributes = """{"locale":["en"],"newsletter":["yes"]}""")
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id")
            HttpClient(CIO).use { http ->
                KeycloakClient(testConfig().keycloak, http).setLocale(id, AppLocale.JA_LATN.key)
            }
            kc.verify(
                putRequestedFor(urlEqualTo("$ADMIN/users/$id"))
                    .withRequestBody(matchingJsonPath("$.attributes.locale[0]", equalTo("ja_latn")))
                    .withRequestBody(matchingJsonPath("$.attributes.newsletter[0]", equalTo("yes")))
                    .withRequestBody(matchingJsonPath("$.email", equalTo("user@example.com"))),
            )
        }
}
