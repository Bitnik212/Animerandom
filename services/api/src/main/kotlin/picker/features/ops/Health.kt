package picker.features.ops

import io.ktor.client.HttpClient
import io.ktor.client.request.get
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import io.ktor.http.isSuccess
import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.Serializable
import picker.infra.db.Db
import picker.infra.redis.Redis

/** One dependency check for /readyz. [required] ones decide the status code. */
class HealthCheck(val name: String, val required: Boolean, val check: suspend () -> Unit)

@Serializable
data class Readiness(val status: String, val checks: Map<String, String>)

fun healthChecks(
    db: Db,
    redis: Redis,
    http: HttpClient,
    elasticsearchUrl: String,
    certsUrl: String,
    recEngineUrl: String,
): List<HealthCheck> =
    listOf(
        HealthCheck("postgres", true) {
            db.dataSource.connection.use { it.createStatement().execute("SELECT 1") }
        },
        HealthCheck("redis", true) { check(redis.commands.ping() == "PONG") },
        HealthCheck("elasticsearch", true) {
            val response = http.get("${elasticsearchUrl.trimEnd('/')}/_cluster/health")
            check(response.status.isSuccess()) { "HTTP ${response.status.value}" }
        },
        HealthCheck("jwks", true) {
            val response = http.get(certsUrl)
            check(response.status.isSuccess() && response.bodyAsText().contains("\"kid\"")) { "no signing keys" }
        },
        HealthCheck("rec-engine", false) {
            val response = http.get("${recEngineUrl.trimEnd('/')}/healthz")
            check(response.status.isSuccess()) { "HTTP ${response.status.value}" }
        },
    )

fun Route.opsRoutes(checks: List<HealthCheck>) {
    get("/healthz") { call.respond(mapOf("status" to "ok")) }
    get("/readyz") {
        val results =
            coroutineScope {
                checks
                    .map { c ->
                        async {
                            val outcome =
                                withTimeoutOrNull(2_000) {
                                    runCatching { c.check() }.fold(
                                        { "ok" },
                                        { "error: ${it.message ?: it::class.simpleName}" },
                                    )
                                } ?: "error: timeout"
                            c to outcome
                        }
                    }.map { it.await() }
            }
        val ready = results.all { (c, outcome) -> !c.required || outcome == "ok" }
        call.respond(
            if (ready) HttpStatusCode.OK else HttpStatusCode.ServiceUnavailable,
            Readiness(if (ready) "ok" else "error", results.associate { (c, o) -> c.name to o }),
        )
    }
}
