package picker.infra.rec

import io.ktor.client.HttpClient
import io.ktor.client.plugins.timeout
import io.ktor.client.request.get
import io.ktor.client.request.parameter
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import io.ktor.http.isSuccess
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import org.slf4j.LoggerFactory
import picker.errors.Errors

@Serializable
data class SimilarItem(
    @SerialName("anime_id") val animeId: Long,
    val score: Double,
)

@Serializable
data class SimilarResponse(val items: List<SimilarItem>)

/**
 * The rec engine's internal HTTP API (services/rec-engine/README.md "Contract with the API").
 * Every call is bounded by `REC_ENGINE_TIMEOUT_MS`; unknown response fields are ignored.
 */
class RecEngineClient(private val http: HttpClient, baseUrl: String, private val timeoutMs: Long) {
    private val log = LoggerFactory.getLogger(RecEngineClient::class.java)
    private val base = baseUrl.trimEnd('/')
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * Similar anime IDs, best first. An unknown anime is `anime-not-found`; the engine being
     * slow or down yields `null`, and the caller decides what to show instead.
     */
    suspend fun similar(animeId: Long, limit: Int, includeAdult: Boolean): List<Long>? {
        val response =
            try {
                http.get("$base/v1/similar/$animeId") {
                    parameter("limit", limit)
                    if (includeAdult) parameter("includeAdult", "true")
                    timeout { requestTimeoutMillis = timeoutMs }
                }
            } catch (e: Exception) {
                log.warn("Rec engine similar({}) failed: {}", animeId, e.toString())
                return null
            }
        if (response.status == HttpStatusCode.NotFound) throw Errors.animeNotFound(animeId)
        if (!response.status.isSuccess()) {
            log.warn("Rec engine similar({}) returned HTTP {}", animeId, response.status.value)
            return null
        }
        return json.decodeFromString(SimilarResponse.serializer(), response.bodyAsText()).items.map { it.animeId }
    }
}
