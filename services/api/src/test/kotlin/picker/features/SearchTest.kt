package picker.features

import io.kotest.matchers.collections.shouldContainExactly
import io.kotest.matchers.shouldBe
import io.ktor.client.request.get
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.support.ApiTest
import picker.support.CatalogStubs
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class SearchTest : ApiTest() {
    private val aggregations =
        """
        {"genres":{"doc_count":2,"values":{"buckets":[{"key":"action","doc_count":2},{"key":"drama","doc_count":1}]}},
         "formats":{"doc_count":2,"values":{"buckets":[{"key":"TV","doc_count":2}]}},
         "decades":{"doc_count":2,"values":{"buckets":[{"key":"2000s","doc_count":1},{"key":"2010s","doc_count":1}]}}}
        """.trimIndent()

    private suspend fun HttpResponse.obj(): JsonObject = bodyAsText().json().jsonObject

    private fun JsonElement.path(vararg keys: String): JsonElement? =
        keys.fold(this as JsonElement?) { node, key -> (node as? JsonObject)?.get(key) }

    @Test
    fun `results keep Elasticsearch's order, with localized facets`() =
        api { client ->
            CatalogStubs.esHits(listOf(2, 1), total = 2, aggregations = aggregations)
            val body = client.get("/v1/search?q=titan&lang=ru").obj()
            body["items"]!!.jsonArray.map { it.jsonObject["title"]!!.jsonPrimitive.content } shouldContainExactly
                listOf("Attack on Titan Season 2", "Атака титанов")
            body["items"]!!
                .jsonArray
                .first()
                .jsonObject
                .containsKey("synopsis") shouldBe false
            body["total"]!!.jsonPrimitive.content shouldBe "2"
            body.path("facets", "genres").toString() shouldBe
                """[{"slug":"action","name":"Экшен","count":2},{"slug":"drama","name":"Драма","count":1}]"""
            body
                .path(
                    "facets",
                    "decades",
                )!!
                .jsonArray
                .map { it.jsonObject["value"]!!.jsonPrimitive.content } shouldContainExactly
                listOf("2010s", "2000s")
        }

    @Test
    fun `the query covers every title field, fuzzy only outside Japanese, with a locale boost`() =
        api { client ->
            CatalogStubs.esHits(emptyList(), aggregations = aggregations)
            client.get("/v1/search?q=atack&lang=ru")
            val request = CatalogStubs.esRequests().single()
            val text =
                request
                    .path("query", "bool", "must")!!
                    .jsonArray
                    .single()
                    .path("bool", "should")!!
                    .jsonArray
            val (fuzzy, exact) = text.map { it.path("multi_match")!!.jsonObject }
            fuzzy["fuzziness"]!!.jsonPrimitive.content shouldBe "AUTO"
            fuzzy["fields"].toString() shouldBe """["title.en^3","title.ru^3","title.ja_latn^3","synonyms^2"]"""
            exact.containsKey("fuzziness") shouldBe false
            exact["fields"].toString() shouldBe """["title.ja^3","synonyms.ja^2"]"""
            request.path("query", "bool", "should").toString().contains("title.ru") shouldBe true
            request["sort"]!!
                .jsonArray
                .first()
                .jsonPrimitive.content shouldBe "_score"
        }

    @Test
    fun `facet filters are post-filters, and each facet ignores its own filter`() =
        api { client ->
            CatalogStubs.esHits(emptyList(), aggregations = aggregations)
            client.get("/v1/search?genre=action&format=TV&tag=military&status=finished")
            val request = CatalogStubs.esRequests().single()
            request.path("post_filter").toString() shouldBe
                """{"bool":{"filter":[{"term":{"genres":"action"}},{"terms":{"format":["TV"]}}]}}"""
            val filters = request.path("query", "bool", "filter").toString()
            filters.contains("genres") shouldBe false
            filters.contains("""{"term":{"tags":"military"}}""") shouldBe true
            filters.contains("""{"terms":{"status":["FINISHED"]}}""") shouldBe true
            request.path("aggs", "genres", "filter").toString() shouldBe
                """{"bool":{"filter":[{"terms":{"format":["TV"]}}]}}"""
            request.path("aggs", "formats", "filter").toString() shouldBe
                """{"bool":{"filter":[{"term":{"genres":"action"}}]}}"""
            request.path("aggs", "decades", "filter").toString().contains("genres") shouldBe true
            request["sort"]!!
                .jsonArray
                .first()
                .path("popularity", "order")!!
                .jsonPrimitive.content shouldBe "desc"
        }

    @Test
    fun `paging, sorting and hideWatched`() =
        api { client ->
            CatalogStubs.esHits(emptyList(), aggregations = aggregations)
            val user = UUID.randomUUID()
            TestInfra.sql("INSERT INTO app.app_user (id) VALUES ('$user')")
            TestInfra.sql("INSERT INTO app.user_anime (user_id, anime_id, status) VALUES ('$user', 9, 'dropped')")
            client.get(
                "/v1/search?page=3&size=10&sort=newest&hideWatched=true",
            ) { bearer(FakeKeycloak.token(sub = user)) }
            val request = CatalogStubs.esRequests().single()
            request["from"]!!.jsonPrimitive.content shouldBe "20"
            request["size"]!!.jsonPrimitive.content shouldBe "10"
            request["sort"]!!
                .jsonArray
                .first()
                .jsonObject.keys shouldBe setOf("season_year")
            request.path("query", "bool", "must_not").toString().contains("""{"terms":{"id":[9]}}""") shouldBe true
        }

    @Test
    fun `invalid search parameters are 400`() =
        api { client ->
            listOf("size=51", "page=0", "page=500&size=50", "sort=best", "episodesMin=-1", "yearFrom=1800")
                .forEach {
                    client
                        .get("/v1/search?$it")
                        .obj()["type"]!!
                        .jsonPrimitive.content shouldBe
                        "validation-failed"
                }
        }

    @Test
    fun `suggest matches prefixes in any script and returns up to 8 short cards`() =
        api { client ->
            CatalogStubs.esHits(listOf(9, 3))
            val body = client.get("/v1/search/suggest?q=De&lang=ja").obj()
            body["items"]!!.jsonArray.map { it.jsonObject["title"]!!.jsonPrimitive.content } shouldContainExactly
                listOf("デスノート", "STEINS;GATE")
            val request = CatalogStubs.esRequests().single()
            request["size"]!!.jsonPrimitive.content shouldBe "8"
            request.toString().contains(""""type":"bool_prefix"""") shouldBe true
            request.toString().contains("title_suggest.ja") shouldBe true
            client
                .get("/v1/search/suggest?q=%20")
                .obj()["type"]!!
                .jsonPrimitive.content shouldBe "validation-failed"
        }
}
