package picker.auth

import io.kotest.matchers.shouldBe
import io.ktor.client.request.get
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.response.respond
import io.ktor.server.routing.get
import io.ktor.server.routing.routing
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.MethodSource
import picker.protectedRoutes
import picker.support.ApiTest
import picker.support.AppSetup
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import picker.support.obj
import java.util.UUID

class TokenValidationTest : ApiTest() {
    private val whoAmI: AppSetup = {
        routing {
            protectedRoutes(AppUserProvisioner { }) {
                get("/test/me") { call.respond(mapOf("id" to call.user().id.toString())) }
                get("/test/admin") { call.respond(mapOf("id" to call.requireAdmin().id.toString())) }
            }
        }
    }

    @Test
    fun `a valid access token is accepted`() =
        api(extra = whoAmI) { client ->
            val sub = UUID.randomUUID()
            val response = client.get("/test/me") { bearer(FakeKeycloak.token(sub = sub)) }
            response.status shouldBe HttpStatusCode.OK
            response
                .bodyAsText()
                .json()
                .obj["id"]!!
                .jsonPrimitive.content shouldBe sub.toString()
        }

    @ParameterizedTest(name = "{0}")
    @MethodSource("rejections")
    fun `every validation rule rejects on its own`(case: String, token: () -> String) =
        api(extra = whoAmI) { client ->
            val response = client.get("/test/me") { bearer(token()) }
            response.status shouldBe HttpStatusCode.Unauthorized
            response.headers[HttpHeaders.WWWAuthenticate] shouldBe "Bearer"
            response.headers[HttpHeaders.ContentType] shouldBe "application/problem+json"
            response
                .bodyAsText()
                .json()
                .obj["type"]!!
                .jsonPrimitive.content shouldBe "unauthorized"
        }

    @Test
    fun `expiry within the leeway is still accepted`() =
        api(extra = whoAmI) { client ->
            client.get("/test/me") { bearer(FakeKeycloak.token(expiresIn = -10)) }.status shouldBe HttpStatusCode.OK
        }

    @Test
    fun `a missing token is 401`() =
        api(extra = whoAmI) { client ->
            client.get("/test/me").status shouldBe HttpStatusCode.Unauthorized
        }

    @Test
    fun `admin routes need the admin role`() =
        api(extra = whoAmI) { client ->
            client.get("/test/admin") { bearer(FakeKeycloak.token()) }.status shouldBe HttpStatusCode.Forbidden
            client.get("/test/admin") { bearer(FakeKeycloak.token(roles = listOf("user", "admin"))) }.status shouldBe
                HttpStatusCode.OK
        }

    @Test
    fun `an invalid token on a public route is 401, not a silent downgrade`() =
        api { client ->
            client.get("/v1/meta/genres").status shouldBe HttpStatusCode.OK
            client.get("/v1/meta/genres") { bearer(FakeKeycloak.token(issuer = "http://evil")) }.status shouldBe
                HttpStatusCode.Unauthorized
        }

    @Test
    fun `the first authenticated request provisions the app_user row`() =
        api { client ->
            val sub = UUID.randomUUID()
            client.get("/v1/meta/genres") { bearer(FakeKeycloak.token(sub = sub)) }.status shouldBe HttpStatusCode.OK
            TestInfra.sql("SELECT locale FROM app.app_user WHERE id = '$sub'") shouldBe listOf(mapOf("locale" to "en"))
            client.get("/v1/meta/genres") { bearer(FakeKeycloak.token(sub = sub)) }.status shouldBe HttpStatusCode.OK
        }

    companion object {
        @JvmStatic
        fun rejections() =
            listOf(
                arrayOf("wrong iss", { FakeKeycloak.token(issuer = "http://keycloak:8080/realms/anime-picker") }),
                arrayOf("wrong aud", { FakeKeycloak.token(audience = listOf("account")) }),
                arrayOf("wrong azp", { FakeKeycloak.token(azp = "other-client") }),
                arrayOf("expired", { FakeKeycloak.token(expiresIn = -120) }),
                arrayOf("not yet valid", { FakeKeycloak.token(notBeforeOffset = 120) }),
                arrayOf("unknown kid", { FakeKeycloak.token(kid = "rotated-away") }),
                arrayOf("bad signature", { FakeKeycloak.token(signWithOtherKey = true) }),
                arrayOf("ID token", { FakeKeycloak.token(typ = "ID") }),
                arrayOf("refresh token", { FakeKeycloak.token(typ = "Refresh") }),
                arrayOf("garbage", { "not-a-jwt" }),
            )
    }
}
