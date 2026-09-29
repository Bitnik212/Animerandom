package picker.features.auth

import io.kotest.assertions.withClue
import io.kotest.matchers.collections.shouldContain
import io.kotest.matchers.collections.shouldNotContain
import io.kotest.matchers.shouldBe
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.Test
import picker.auth.KeycloakClient
import picker.config.KeycloakConfig
import picker.config.RateLimit
import picker.support.ApiTest
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import picker.support.testConfig
import java.util.UUID

/**
 * Runs the auth flows against a real Keycloak with `infra/keycloak/realm-anime-picker.json` imported.
 * Skipped unless `API_TEST_KEYCLOAK_URL` and `API_TEST_KEYCLOAK_SECRET` are set.
 */
class RealKeycloakTest : ApiTest() {
    private val url = System.getenv("API_TEST_KEYCLOAK_URL")
    private val secret = System.getenv("API_TEST_KEYCLOAK_SECRET")

    private fun config() =
        testConfig {
            copy(
                // One email goes through many limited calls here; the limiter has its own tests.
                authRateLimitEmail = RateLimit(20, 60),
                keycloak =
                    KeycloakConfig(
                        internalUrl = url,
                        issuer = System.getenv("API_TEST_KEYCLOAK_ISSUER") ?: "$url/realms/anime-picker",
                        realm = "anime-picker",
                        clientId = "anime-picker-api",
                        clientSecret = secret,
                    ),
            )
        }

    private suspend fun HttpClient.postJson(path: String, body: String): HttpResponse =
        post(path) {
            contentType(ContentType.Application.Json)
            setBody(body)
        }

    private suspend fun HttpClient.signIn(email: String, password: String) =
        postJson("/v1/auth/signin", """{"email":"$email","password":"$password"}""")

    private suspend fun HttpClient.signUp(email: String, password: String) =
        postJson("/v1/auth/signup", """{"email":"$email","password":"$password","locale":"ru"}""")

    private suspend fun HttpResponse.field(name: String) =
        bodyAsText()
            .json()
            .jsonObject[name]!!
            .jsonPrimitive.content

    private suspend fun HttpResponse.problem() = field("type")

    @Test
    fun `sign-up to deletion against the real realm`() {
        assumeTrue(!url.isNullOrBlank() && !secret.isNullOrBlank(), "API_TEST_KEYCLOAK_URL/SECRET not set")
        api(config()) { client ->
            val email = "it-${UUID.randomUUID()}@example.com"
            val signUp = client.signUp(email, "first-pass-1")
            withClue(signUp.bodyAsText()) { signUp.status shouldBe HttpStatusCode.Created }

            // Realm checks: duplicate email and the password policy.
            client.signUp(email, "first-pass-1").problem() shouldBe "email-taken"
            val other = "it-${UUID.randomUUID()}@example.com"
            client.signUp(other, other).problem() shouldBe "weak-password"

            // The access token passes local validation (aud mapper, azp, iss) on a protected route.
            client
                .put("/v1/users/me/password") {
                    bearer(signUp.field("accessToken"))
                    contentType(ContentType.Application.Json)
                    setBody("""{"currentPassword":"first-pass-1","newPassword":"second-pass-2"}""")
                }.status shouldBe HttpStatusCode.NoContent
            client.signIn(email, "first-pass-1").problem() shouldBe "invalid-credentials"
            val signIn = client.signIn(email, "second-pass-2")
            signIn.status shouldBe HttpStatusCode.OK
            val refresh = """{"refreshToken":"${signIn.field("refreshToken")}"}"""

            client.postJson("/v1/auth/refresh", refresh).status shouldBe HttpStatusCode.OK
            client.postJson("/v1/auth/signout", refresh).status shouldBe HttpStatusCode.NoContent
            client.postJson("/v1/auth/refresh", refresh).problem() shouldBe "unauthorized"
            client.postJson("/v1/auth/password/forgot", """{"email":"$email"}""").status shouldBe
                HttpStatusCode.Accepted

            // Service-account permissions: roles and the locale attribute.
            val id = UUID.fromString(TestInfra.sql("SELECT id::text AS id FROM app.app_user")[0]["id"] as String)
            HttpClient(CIO).use { http ->
                val keycloak = KeycloakClient(config().keycloak, http)
                keycloak.realmRoles(id) shouldContain "user"
                keycloak.setRealmRole(id, "admin", granted = true)
                keycloak.setLocale(id, "ja")
                val attributes = keycloak.getUser(id)["attributes"]!!.jsonObject
                attributes["locale"]!!.jsonArray[0].jsonPrimitive.content shouldBe "ja"
            }

            // A fresh token carries the admin role.
            val access = client.signIn(email, "second-pass-2").field("accessToken")
            val view = client.get("/v1/admin/users/$id") { bearer(access) }
            view.status shouldBe HttpStatusCode.OK
            view.field("email") shouldBe email
            client.delete("/v1/admin/users/$id/admin") { bearer(access) }.status shouldBe HttpStatusCode.NoContent
            HttpClient(CIO).use { KeycloakClient(config().keycloak, it).realmRoles(id) shouldNotContain "admin" }

            client.post("/v1/users/me/logout-all") { bearer(access) }.status shouldBe HttpStatusCode.NoContent
            client.delete("/v1/users/me") { bearer(access) }.status shouldBe HttpStatusCode.NoContent
            client.signIn(email, "second-pass-2").problem() shouldBe "invalid-credentials"
        }
    }
}
