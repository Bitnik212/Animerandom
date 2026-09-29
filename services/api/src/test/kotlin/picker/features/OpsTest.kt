package picker.features

import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import io.kotest.matchers.shouldBe
import io.kotest.matchers.string.shouldStartWith
import io.ktor.client.request.header
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import io.ktor.server.plugins.origin
import io.ktor.server.response.respondText
import io.ktor.server.routing.get
import io.ktor.server.routing.routing
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import org.koin.dsl.module
import picker.infra.db.Db
import picker.support.ApiTest
import picker.support.AppSetup
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.json
import picker.support.testConfig
import java.net.ServerSocket
import kotlin.concurrent.thread
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

    @Test
    fun `a hung Postgres fails readiness within the timeout instead of hanging`() {
        // Accepts TCP connections but never answers the Postgres handshake.
        ServerSocket(0).use { blackHole ->
            val accepted = mutableListOf<java.net.Socket>()
            val acceptor = thread(isDaemon = true) { runCatching { while (true) accepted += blackHole.accept() } }
            val hung =
                HikariConfig().apply {
                    jdbcUrl = "jdbc:postgresql://127.0.0.1:${blackHole.localPort}/anime"
                    username = "x"
                    initializationFailTimeout = -1
                    connectionTimeout = 30_000
                    maximumPoolSize = 1
                }
            api(
                testConfig {
                    copy(runMigrations = false)
                },
                overrides = module { single { Db(HikariDataSource(hung)) } },
            ) { client ->
                val started = System.nanoTime()
                val body =
                    client
                        .httpGet("/readyz")
                        .bodyAsText()
                        .json()
                        .jsonObject
                val seconds = (System.nanoTime() - started) / 1e9
                body["checks"]!!.jsonObject["postgres"]!!.jsonPrimitive.content shouldBe "error: timeout"
                (seconds < 5) shouldBe true
            }
            accepted.forEach { it.close() }
            acceptor.interrupt()
        }
    }

    @Test
    fun `forwarded headers can't choose the client IP unless a proxy is trusted`() {
        val whoami: AppSetup = { routing { get("/whoami") { call.respondText(call.request.origin.remoteHost) } } }
        api(extra = whoami) { client ->
            client.httpGet("/whoami") { header("X-Forwarded-For", "203.0.113.9") }.bodyAsText() shouldBe "localhost"
        }
        api(testConfig { copy(trustForwardedHeaders = true) }, extra = whoami) { client ->
            // Only the hop the proxy added counts, not what the client put in front of it.
            client.httpGet("/whoami") { header("X-Forwarded-For", "198.51.100.1, 203.0.113.9") }.bodyAsText() shouldBe
                "203.0.113.9"
        }
    }
}
