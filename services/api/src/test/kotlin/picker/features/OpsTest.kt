package picker.features

import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import io.kotest.matchers.shouldBe
import io.kotest.matchers.string.shouldStartWith
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.support.ApiTest
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.json
import io.ktor.client.request.get as httpGet

class OpsTest : ApiTest() {
    @Test
    fun `healthz checks nothing`() =
        api { client ->
            client.httpGet("/healthz").status shouldBe HttpStatusCode.OK
        }

    @Test
    fun `readyz needs postgres, redis, elasticsearch and jwks - not the rec engine`() =
        api { client ->
            val down = client.httpGet("/readyz")
            down.status shouldBe HttpStatusCode.ServiceUnavailable
            val checks =
                down
                    .bodyAsText()
                    .json()
                    .jsonObject["checks"]!!
                    .jsonObject
            checks["postgres"]!!.jsonPrimitive.content shouldBe "ok"
            checks["redis"]!!.jsonPrimitive.content shouldBe "ok"
            checks["jwks"]!!.jsonPrimitive.content shouldBe "ok"
            checks["elasticsearch"]!!.jsonPrimitive.content shouldStartWith "error"

            FakeKeycloak.server.stubFor(get(urlEqualTo("/es/_cluster/health")).willReturn(aResponse().withBody("{}")))
            val up = client.httpGet("/readyz")
            up.status shouldBe HttpStatusCode.OK // rec engine still down, but not required
            up
                .bodyAsText()
                .json()
                .jsonObject["checks"]!!
                .jsonObject["rec-engine"]!!
                .jsonPrimitive.content shouldStartWith
                "error"
        }

    @Test
    fun `flyway owns schema app and its history`() =
        api { _ ->
            val tables =
                TestInfra
                    .sql(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'app' ORDER BY table_name",
                    ).map { it["table_name"] }
            tables shouldBe listOf("app_user", "flyway_schema_history", "user_anime", "user_feedback")
        }
}
