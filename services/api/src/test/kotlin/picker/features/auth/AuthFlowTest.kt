package picker.features.auth

import com.github.tomakehurst.wiremock.client.WireMock.containing
import com.github.tomakehurst.wiremock.client.WireMock.deleteRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.equalToJson
import com.github.tomakehurst.wiremock.client.WireMock.exactly
import com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath
import com.github.tomakehurst.wiremock.client.WireMock.notContaining
import com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.putRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import io.kotest.matchers.shouldBe
import io.kotest.matchers.shouldNotBe
import io.ktor.client.HttpClient
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.config.RateLimit
import picker.support.ApiTest
import picker.support.FakeKeycloak
import picker.support.KeycloakStubs
import picker.support.KeycloakStubs.ADMIN
import picker.support.TestInfra
import picker.support.json
import picker.support.testConfig
import java.util.UUID

class AuthFlowTest : ApiTest() {
    private val kc get() = FakeKeycloak.server

    private suspend fun HttpClient.postJson(path: String, body: String): HttpResponse =
        post(path) {
            contentType(ContentType.Application.Json)
            setBody(body)
        }

    private fun HttpResponse.type(): Unit = Unit

    private suspend fun HttpResponse.problemType() =
        bodyAsText()
            .json()
            .jsonObject["type"]!!
            .jsonPrimitive.content

    private val signUp = """{"email":"New@Example.com","password":"correct horse","displayName":"Neo","locale":"ru"}"""

    @Test
    fun `sign-up creates the Keycloak user, the app_user row, and signs in`() =
        api { client ->
            val id = UUID.randomUUID()
            KeycloakStubs.serviceAccount()
            KeycloakStubs.createUser(id)
            KeycloakStubs.passwordGrant()

            val response = client.postJson("/v1/auth/signup", signUp)
            response.status shouldBe HttpStatusCode.Created
            val body = response.bodyAsText().json().jsonObject
            body["accessToken"]!!.jsonPrimitive.content shouldBe "access-token"
            body["refreshExpiresIn"]!!.jsonPrimitive.content shouldBe "2592000"
            body["tokenType"]!!.jsonPrimitive.content shouldBe "Bearer"

            kc.verify(
                postRequestedFor(urlEqualTo("$ADMIN/users"))
                    .withHeader(
                        HttpHeaders.Authorization,
                        com.github.tomakehurst.wiremock.client.WireMock
                            .equalTo("Bearer service-token"),
                    ).withRequestBody(
                        matchingJsonPath(
                            "$.username",
                            com.github.tomakehurst.wiremock.client.WireMock
                                .equalTo("new@example.com"),
                        ),
                    ).withRequestBody(
                        matchingJsonPath(
                            "$.attributes.locale[0]",
                            com.github.tomakehurst.wiremock.client.WireMock
                                .equalTo("ru"),
                        ),
                    ).withRequestBody(notContaining("Neo")), // displayName stays in app_user
            )
            TestInfra.sql("SELECT display_name, locale FROM app.app_user WHERE id = '$id'") shouldBe
                listOf(mapOf("display_name" to "Neo", "locale" to "ru"))
        }

    @Test
    fun `sign-up maps Keycloak's conflict and policy errors`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.createUserFails(409, """{"errorMessage":"User exists with same username"}""")
            client.postJson("/v1/auth/signup", signUp).let {
                it.status shouldBe HttpStatusCode.Conflict
                it.problemType() shouldBe "email-taken"
            }
            KeycloakStubs.createUserFails(
                400,
                """{"error":"invalidPasswordNotUsernameMessage","errorMessage":"Invalid password"}""",
            )
            client.postJson("/v1/auth/signup", signUp).problemType() shouldBe "weak-password"
        }

    @Test
    fun `sign-up validates locally before calling Keycloak`() =
        api { client ->
            client.postJson("/v1/auth/signup", """{"email":"x@y.z","password":"short"}""").problemType() shouldBe
                "weak-password"
            client
                .postJson(
                    "/v1/auth/signup",
                    """{"email":"not-an-email","password":"long enough"}""",
                ).problemType() shouldBe
                "validation-failed"
            client
                .postJson(
                    "/v1/auth/signup",
                    """{"email":"x@y.z","password":"long enough","locale":"de"}""",
                ).problemType() shouldBe
                "unsupported-locale"
            client.postJson("/v1/auth/signup", """{"email":"x@y.z"}""").problemType() shouldBe "validation-failed"
            kc.verify(exactly(0), postRequestedFor(urlEqualTo("$ADMIN/users")))
        }

    @Test
    fun `sign-up rolls the Keycloak user back when app_user can't be written`() =
        api { client ->
            val id = UUID.randomUUID()
            TestInfra.sql("INSERT INTO app.app_user (id) VALUES ('$id')") // collides with the new row
            KeycloakStubs.serviceAccount()
            KeycloakStubs.createUser(id)
            KeycloakStubs.ok("DELETE", "$ADMIN/users/$id")
            client.postJson("/v1/auth/signup", signUp).status shouldBe HttpStatusCode.InternalServerError
            kc.verify(deleteRequestedFor(urlEqualTo("$ADMIN/users/$id")))
        }

    @Test
    fun `with verification required, sign-up returns no tokens`() =
        api(testConfig { copy(emailVerificationRequired = true) }) { client ->
            val id = UUID.randomUUID()
            KeycloakStubs.serviceAccount()
            KeycloakStubs.createUser(id)
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id/send-verify-email")
            val response = client.postJson("/v1/auth/signup", signUp)
            response.status shouldBe HttpStatusCode.Created
            response.bodyAsText().json() shouldBe """{"verificationRequired":true}""".json()
            kc.verify(
                exactly(0),
                postRequestedFor(urlEqualTo(KeycloakStubs.TOKEN)).withRequestBody(containing("grant_type=password")),
            )
        }

    @Test
    fun `sign-in maps every Keycloak outcome`() =
        api { client ->
            val body = """{"email":"a@b.c","password":"secret-password"}"""
            KeycloakStubs.passwordGrant()
            client.postJson("/v1/auth/signin", body).status shouldBe HttpStatusCode.OK

            KeycloakStubs.invalidGrant("Invalid user credentials")
            client.postJson("/v1/auth/signin", body).let {
                it.status shouldBe HttpStatusCode.Unauthorized
                it.problemType() shouldBe "invalid-credentials"
            }
            KeycloakStubs.invalidGrant("Account is not fully set up")
            client.postJson("/v1/auth/signin", body).problemType() shouldBe "account-not-ready"
            KeycloakStubs.invalidGrant("Account disabled")
            client.postJson("/v1/auth/signin", body).problemType() shouldBe "account-not-ready"
            KeycloakStubs.passwordGrant(503, "")
            client.postJson("/v1/auth/signin", body).let {
                it.status shouldBe HttpStatusCode.BadGateway
                it.problemType() shouldBe "identity-provider-unavailable"
            }
        }

    @Test
    fun `sign-in with Keycloak down is 502`() =
        api(testConfig { copy(keycloak = keycloak.copy(internalUrl = "http://127.0.0.1:9")) }) { client ->
            client
                .postJson(
                    "/v1/auth/signin",
                    """{"email":"a@b.c","password":"secret-password"}""",
                ).problemType() shouldBe
                "identity-provider-unavailable"
        }

    @Test
    fun `refresh and sign-out`() =
        api { client ->
            KeycloakStubs.refreshGrant()
            client
                .postJson("/v1/auth/refresh", """{"refreshToken":"r1"}""")
                .bodyAsText()
                .json()
                .jsonObject["accessToken"]!!
                .jsonPrimitive.content shouldBe "access-2"
            KeycloakStubs.refreshGrant(400, """{"error":"invalid_grant","error_description":"Token is not active"}""")
            client.postJson("/v1/auth/refresh", """{"refreshToken":"r1"}""").problemType() shouldBe "unauthorized"

            KeycloakStubs.logout()
            client.postJson("/v1/auth/signout", """{"refreshToken":"r1"}""").status shouldBe HttpStatusCode.NoContent
            kc.verify(
                postRequestedFor(urlEqualTo(KeycloakStubs.LOGOUT)).withRequestBody(containing("refresh_token=r1")),
            )
        }

    @Test
    fun `forgot password is always 202`() =
        api { client ->
            val id = UUID.randomUUID()
            KeycloakStubs.serviceAccount()
            KeycloakStubs.findByEmail("known@example.com", id)
            KeycloakStubs.ok("PUT", "$ADMIN/users/$id/execute-actions-email")
            KeycloakStubs.findByEmail("unknown@example.com", null)

            client.postJson("/v1/auth/password/forgot", """{"email":"known@example.com"}""").status shouldBe
                HttpStatusCode.Accepted
            kc.verify(
                putRequestedFor(
                    urlEqualTo("$ADMIN/users/$id/execute-actions-email"),
                ).withRequestBody(equalToJson("""["UPDATE_PASSWORD"]""")),
            )
            client.postJson("/v1/auth/password/forgot", """{"email":"unknown@example.com"}""").status shouldBe
                HttpStatusCode.Accepted
            client.postJson("/v1/auth/password/forgot", """{"email":"garbage"}""").status shouldBe
                HttpStatusCode.Accepted
            kc.resetMappings()
            FakeKeycloak.stubJwks()
            client.postJson("/v1/auth/password/forgot", """{"email":"known@example.com"}""").status shouldBe
                HttpStatusCode.Accepted
        }

    @Test
    fun `sign-in attempts are rate limited per email and per IP`() =
        api(testConfig { copy(authRateLimitEmail = RateLimit(2, 60), authRateLimitIp = RateLimit(4, 60)) }) { client ->
            KeycloakStubs.invalidGrant("Invalid user credentials")
            repeat(2) {
                client.postJson("/v1/auth/signin", """{"email":"a@b.c","password":"wrong-pass"}""").status shouldBe
                    HttpStatusCode.Unauthorized
            }
            val limited = client.postJson("/v1/auth/signin", """{"email":"A@b.c ","password":"wrong-pass"}""")
            limited.status shouldBe HttpStatusCode.TooManyRequests
            limited.problemType() shouldBe "too-many-attempts"
            limited.headers[HttpHeaders.RetryAfter]!!.toLong() shouldNotBe 0L

            client.postJson("/v1/auth/signin", """{"email":"other@b.c","password":"wrong-pass"}""").status shouldBe
                HttpStatusCode.Unauthorized
            // The IP limit (4) now counts 4 attempts; the 5th from this address is refused whatever the email.
            client.postJson("/v1/auth/signin", """{"email":"third@b.c","password":"wrong-pass"}""").status shouldBe
                HttpStatusCode.TooManyRequests
        }

    @Test
    fun `forgot password is limited per email, whatever the address`() =
        api(testConfig { copy(authRateLimitEmail = RateLimit(2, 60)) }) { client ->
            KeycloakStubs.serviceAccount()
            KeycloakStubs.findByEmail("victim@example.com", null)
            repeat(2) {
                client.postJson("/v1/auth/password/forgot", """{"email":"victim@example.com"}""").status shouldBe
                    HttpStatusCode.Accepted
            }
            client.postJson("/v1/auth/password/forgot", """{"email":"Victim@Example.com"}""").status shouldBe
                HttpStatusCode.TooManyRequests
            client.postJson("/v1/auth/password/forgot", """{"email":"other@example.com"}""").status shouldBe
                HttpStatusCode.Accepted
        }

    @Test
    fun `the service-account token is reused until it nearly expires`() =
        api { client ->
            KeycloakStubs.serviceAccount()
            repeat(3) {
                val email = "u$it@example.com"
                KeycloakStubs.findByEmail(email, null)
                client.postJson("/v1/auth/password/forgot", """{"email":"$email"}""")
            }
            kc.verify(
                exactly(1),
                postRequestedFor(urlEqualTo(KeycloakStubs.TOKEN)).withRequestBody(containing("client_credentials")),
            )
        }
}
