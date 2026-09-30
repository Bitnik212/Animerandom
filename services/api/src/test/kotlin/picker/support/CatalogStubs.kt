package picker.support

import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.client.WireMock.urlPathEqualTo
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import picker.infra.redis.Keys
import picker.infra.redis.Redis

/** Elasticsearch and rec-engine stand-ins on the shared WireMock server, and pool seeding. */
object CatalogStubs {
    const val ES_SEARCH = "/es/anime/_search"

    fun esHits(ids: List<Long>, total: Long = ids.size.toLong(), aggregations: String? = null) {
        val hits = ids.joinToString(",") { """{"_index":"anime_v1","_id":"$it","_score":1.0}""" }
        val aggs = aggregations?.let { ""","aggregations":$it""" }.orEmpty()
        FakeKeycloak.server.stubFor(
            post(urlEqualTo(ES_SEARCH)).willReturn(
                KeycloakStubs.json(
                    200,
                    """{"took":1,"hits":{"total":{"value":$total,"relation":"eq"},"hits":[$hits]}$aggs}""",
                ),
            ),
        )
    }

    fun esDown(status: Int = 503) =
        FakeKeycloak.server.stubFor(post(urlEqualTo(ES_SEARCH)).willReturn(aResponse().withStatus(status)))

    /** Bodies of every search request made so far, parsed. */
    fun esRequests(): List<JsonObject> =
        FakeKeycloak.server
            .findAll(postRequestedFor(urlEqualTo(ES_SEARCH)))
            .map { it.bodyAsString.json().jsonObject }

    fun similar(animeId: Long, ids: List<Long>, status: Int = 200, delayMs: Int = 0) {
        val items = ids.joinToString(",") { """{"anime_id":$it,"score":0.9,"extra":"ignored"}""" }
        FakeKeycloak.server.stubFor(
            get(urlPathEqualTo("/rec/v1/similar/$animeId")).willReturn(
                KeycloakStubs
                    .json(status, if (status == 200) """{"items":[$items]}""" else """{"type":"x"}""")
                    .withFixedDelay(delayMs),
            ),
        )
    }

    /** The pools the ingest worker would build from the fixture catalog (no adult #6, no removed #7). */
    fun seedPools() =
        Redis(TestInfra.redisUrl).use { redis ->
            runBlocking {
                val pools =
                    mapOf(
                        Keys.POOL_ALL to listOf(1, 2, 3, 4, 5, 8, 9, 10, 11, 12),
                        Keys.poolGenre("action") to listOf(1, 2, 8, 10, 11),
                        Keys.poolGenre("drama") to listOf(1, 2, 3, 8, 9, 12),
                        Keys.poolGenre("fantasy") to listOf(1, 4, 8, 10, 12),
                        Keys.poolFormat("TV") to listOf(1, 2, 3, 8, 9, 10, 11),
                        Keys.poolFormat("MOVIE") to listOf(4, 12),
                        Keys.poolScore(9) to listOf(3, 8, 10),
                        Keys.poolScore(8) to listOf(1, 2, 3, 8, 9, 10, 11, 12),
                        Keys.poolDecade("2010s") to listOf(1, 2, 3, 4, 10),
                        Keys.poolLength("short") to listOf(2, 4, 12),
                    )
                pools.forEach { (key, ids) -> redis.commands.sadd(key, *ids.map { it.toString() }.toTypedArray()) }
            }
        }
}
