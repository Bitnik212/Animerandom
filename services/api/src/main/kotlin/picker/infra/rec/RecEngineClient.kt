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
import java.util.UUID

@Serializable
data class SimilarItem(
    @SerialName("anime_id") val animeId: Long,
    val score: Double,
)

@Serializable
data class SimilarResponse(val items: List<SimilarItem>)

/** A reason carries only the field of its code: `anime_id` for `similar_to`, `genre` for `popular_in_genre`. */
@Serializable
data class RecReason(
    val code: String,
    @SerialName("anime_id") val animeId: Long? = null,
    val genre: String? = null,
)

@Serializable
data class RecItem(
    @SerialName("anime_id") val animeId: Long,
    val score: Double,
    val reason: RecReason,
)

@Serializable
data class RecResponse(val items: List<RecItem>)

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

    /** The user's ranked feed, or `null` when the engine is slow, down, or answers garbage. */
    suspend fun recommendations(userId: UUID, limit: Int): List<RecItem>? {
        val response =
            try {
                http.get("$base/v1/recommendations/$userId") {
                    parameter("limit", limit)
                    timeout { requestTimeoutMillis = timeoutMs }
                }
            } catch (e: Exception) {
                log.warn("Rec engine recommendations failed: {}", e.toString())
                return null
            }
        if (!response.status.isSuccess()) {
            log.warn("Rec engine recommendations returned HTTP {}", response.status.value)
            return null
        }
        return runCatching { json.decodeFromString(RecResponse.serializer(), response.bodyAsText()).items }
            .onFailure { log.warn("Unreadable rec engine response: {}", it.toString()) }
            .getOrNull()
    }
}
