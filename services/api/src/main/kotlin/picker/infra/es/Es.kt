package picker.infra.es

import io.ktor.client.HttpClient
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.slf4j.LoggerFactory
import picker.errors.Errors

/** One search hit page: anime IDs in rank order, the total, and raw aggregations. */
data class EsResult(val ids: List<Long>, val total: Long, val aggregations: JsonObject?)

/**
 * Read-only access to the `anime` alias (infra/README.md "Elasticsearch"). Queries are plain
 * JSON built with [Q]; this service never writes to Elasticsearch.
 */
class Es(private val http: HttpClient, baseUrl: String, private val index: String = "anime") {
    private val log = LoggerFactory.getLogger(Es::class.java)
    private val searchUrl = "${baseUrl.trimEnd('/')}/$index/_search"
    private val json = Json { ignoreUnknownKeys = true }

    suspend fun search(body: JsonObject): EsResult {
        val response =
            try {
                http.post(searchUrl) {
                    contentType(ContentType.Application.Json)
                    setBody(body.toString())
                }
            } catch (e: Exception) {
                log.warn("Elasticsearch unreachable: {}", e.toString())
                throw Errors.searchUnavailable("Elasticsearch unreachable")
            }
        if (!response.status.isSuccess()) {
            log.warn("Elasticsearch search failed: HTTP {}", response.status.value)
            throw Errors.searchUnavailable("Elasticsearch returned ${response.status.value}")
        }
        val text = response.bodyAsText()
        val root = json.parseToJsonElement(text).jsonObject
        val hits = root["hits"]!!.jsonObject
        val ids =
            hits["hits"]!!.jsonArray.map {
                it.jsonObject["_id"]!!
                    .jsonPrimitive.content
                    .toLong()
            }
        val total =
            hits["total"]
                ?.jsonObject
                ?.get("value")
                ?.jsonPrimitive
                ?.long ?: ids.size.toLong()
        return EsResult(ids, total, root["aggregations"]?.jsonObject)
    }
}

/** Small builders for the query DSL, so query code reads like the JSON it produces. */
object Q {
    fun obj(block: JsonObjectBuilder.() -> Unit): JsonObject = buildJsonObject(block)

    fun arr(items: Iterable<JsonElement>): JsonArray = buildJsonArray { items.forEach { add(it) } }

    fun term(field: String, value: String) = obj { put("term", obj { put(field, JsonPrimitive(value)) }) }

    fun term(field: String, value: Boolean) = obj { put("term", obj { put(field, JsonPrimitive(value)) }) }

    fun terms(field: String, values: Collection<Any>) =
        obj {
            put(
                "terms",
                obj {
                    put(
                        field,
                        arr(values.map { if (it is Number) JsonPrimitive(it) else JsonPrimitive(it.toString()) }),
                    )
                },
            )
        }

    fun range(field: String, gte: Number? = null, lte: Number? = null) =
        obj {
            put(
                "range",
                obj {
                    put(
                        field,
                        obj {
                            gte?.let { put("gte", JsonPrimitive(it)) }
                            lte?.let { put("lte", JsonPrimitive(it)) }
                        },
                    )
                },
            )
        }

    fun bool(
        must: List<JsonObject> = emptyList(),
        filter: List<JsonObject> = emptyList(),
        mustNot: List<JsonObject> = emptyList(),
        should: List<JsonObject> = emptyList(),
        minimumShouldMatch: Int? = null,
    ) = obj {
        put(
            "bool",
            obj {
                if (must.isNotEmpty()) put("must", arr(must))
                if (filter.isNotEmpty()) put("filter", arr(filter))
                if (mustNot.isNotEmpty()) put("must_not", arr(mustNot))
                if (should.isNotEmpty()) put("should", arr(should))
                minimumShouldMatch?.let { put("minimum_should_match", JsonPrimitive(it)) }
            },
        )
    }

    val matchAll: JsonObject get() = obj { put("match_all", obj { }) }
}
