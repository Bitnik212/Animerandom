package picker.support

import io.ktor.client.HttpClient
import io.ktor.client.request.HttpRequestBuilder
import io.ktor.client.request.header
import io.ktor.http.HttpHeaders
import io.ktor.server.testing.ApplicationTestBuilder
import io.ktor.server.testing.testApplication
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.jupiter.api.BeforeEach
import org.koin.core.module.Module
import picker.config.AppConfig
import picker.configure
import picker.infra.redis.Redis

/** Base for tests that run the whole application against the shared test infrastructure. */
abstract class ApiTest {
    @BeforeEach
    fun resetState() {
        FakeKeycloak.server.resetAll()
        FakeKeycloak.stubJwks()
        TestInfra.resetAppData()
        Redis(TestInfra.redisUrl).use { runBlocking { it.commands.flushdb() } }
    }

    fun api(
        config: AppConfig = testConfig(),
        overrides: Module? = null,
        extra: AppSetup = {},
        block: suspend ApplicationTestBuilder.(HttpClient) -> Unit,
    ) = testApplication {
        application {
            configure(config, overrides)
            extra()
        }
        block(client)
    }
}

fun HttpRequestBuilder.bearer(token: String) = header(HttpHeaders.Authorization, "Bearer $token")

fun String.json(): JsonElement = Json.parseToJsonElement(this)

val JsonElement.obj: JsonObject get() = jsonObject
