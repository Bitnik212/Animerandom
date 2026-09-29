package picker.features

import io.kotest.matchers.collections.shouldBeIn
import io.kotest.matchers.collections.shouldBeUnique
import io.kotest.matchers.collections.shouldContain
import io.kotest.matchers.collections.shouldContainExactly
import io.kotest.matchers.collections.shouldContainExactlyInAnyOrder
import io.kotest.matchers.collections.shouldHaveSize
import io.kotest.matchers.shouldBe
import io.ktor.client.HttpClient
import io.ktor.client.request.get
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpStatusCode
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import picker.support.ApiTest
import picker.support.CatalogStubs
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class RandomTest : ApiTest() {
    @BeforeEach
    fun pools() = CatalogStubs.seedPools()

    private data class Pick(val ids: List<Long>, val poolSize: Long)

    private suspend fun HttpClient.pick(query: String, token: String? = null): Pick {
        val response = get("/v1/anime/random?$query") { token?.let { bearer(it) } }
        response.status shouldBe HttpStatusCode.OK
        val body = response.bodyAsText().json().jsonObject
        return Pick(
            body["items"]!!.jsonArray.map {
                it.jsonObject["id"]!!
                    .jsonPrimitive.content
                    .toLong()
            },
            body["poolSize"]!!.jsonPrimitive.content.toLong(),
        )
    }

    private suspend fun HttpResponse.problem() =
        bodyAsText()
            .json()
            .jsonObject["type"]!!
            .jsonPrimitive.content

    private fun user(showAdult: Boolean = false): UUID =
        UUID.randomUUID().also { TestInfra.sql("INSERT INTO app.app_user (id, show_adult) VALUES ('$it', $showAdult)") }

    private fun redisKeys(pattern: String): List<String> =
        Redis(TestInfra.redisUrl).use { runBlocking { it.commands.keys(pattern).toList() } }

    @Test
    fun `pooled filters intersect, and excluded genres are subtracted`() =
        api { client ->
            client.pick("genre=action&genre=drama&count=10").let {
                it.poolSize shouldBe 3
                it.ids shouldContainExactlyInAnyOrder listOf(1L, 2L, 8L)
            }
            client.pick("genre=action&excludeGenre=drama&count=10").ids shouldContainExactlyInAnyOrder listOf(10L, 11L)
            client.pick("format=movie&decade=2010s").ids shouldContainExactly listOf(4L)
            client.pick("length=short&count=10").ids shouldContainExactlyInAnyOrder listOf(2L, 4L, 12L)
            client.pick("minScore=9.5&count=10").ids shouldContainExactlyInAnyOrder listOf(3L, 8L, 10L)
            client.pick("minScore=8.2&genre=fantasy&count=10").ids shouldContainExactlyInAnyOrder
                listOf(1L, 8L, 10L, 12L)
            CatalogStubs.esRequests() shouldHaveSize 0
            redisKeys("tmp:rand:*") shouldBe emptyList() // scratch keys are cleaned up
        }

    @Test
    fun `picks are distinct and come from the whole catalog without filters`() =
        api { client ->
            val pick = client.pick("count=10")
            pick.poolSize shouldBe 10
            pick.ids shouldHaveSize 10
            pick.ids.shouldBeUnique()
            client.pick("").ids.single() shouldBeIn listOf(1L, 2L, 3L, 4L, 5L, 8L, 9L, 10L, 11L, 12L)
        }

    @Test
    fun `an empty pool is a 200 with no items`() =
        api { client ->
            client.pick("genre=romance") shouldBe Pick(emptyList(), 0)
            client.pick("genre=action&genre=drama&minScore=9&length=short") shouldBe Pick(emptyList(), 0)
        }

    @Test
    fun `signed-in users don't get what they watched or rejected, unless they ask`() =
        api { client ->
            val id = user()
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status) VALUES ('$id', 1, 'completed'), ('$id', 8, 'planned')",
            )
            TestInfra.sql("INSERT INTO app.user_feedback (user_id, anime_id, kind) VALUES ('$id', 2, 'not_interested')")
            val token = FakeKeycloak.token(sub = id)
            client.pick("genre=action&genre=drama&count=10", token) shouldBe Pick(listOf(8), 1)
            redisKeys(Keys.userExcluded(id)) shouldContain Keys.userExcluded(id)
            client.pick("genre=action&genre=drama&count=10&hideWatched=false", token).poolSize shouldBe 3
            client.pick("genre=action&genre=drama&count=10").poolSize shouldBe 3 // anonymous: nothing hidden
        }

    @Test
    fun `a user with nothing excluded still gets a cached exclusion set`() =
        api { client ->
            val id = user()
            client.pick("count=10", FakeKeycloak.token(sub = id)).poolSize shouldBe 10
            Redis(
                TestInfra.redisUrl,
            ).use { runBlocking { it.commands.smembers(Keys.userExcluded(id)).toList() } } shouldBe
                listOf("0")
        }

    @Test
    fun `the watchlist source picks from planned titles only`() =
        api { client ->
            client.get("/v1/anime/random?source=watchlist").problem() shouldBe "unauthorized"
            val id = user()
            val token = FakeKeycloak.token(sub = id)
            client.pick("source=watchlist", token) shouldBe Pick(emptyList(), 0)
            TestInfra.sql(
                "INSERT INTO app.user_anime (user_id, anime_id, status) " +
                    "VALUES ('$id', 3, 'planned'), ('$id', 9, 'planned'), ('$id', 4, 'planned'), ('$id', 12, 'completed')",
            )
            client.pick("source=watchlist&genre=drama&count=10", token).ids shouldContainExactlyInAnyOrder
                listOf(3L, 9L)
            redisKeys("tmp:rand:*") shouldBe emptyList()
        }

    @Test
    fun `filters the pools can't answer go to Elasticsearch`() =
        api { client ->
            CatalogStubs.esHits(listOf(3), total = 42)
            client.pick("tag=time-travel&genre=drama&yearFrom=2010") shouldBe Pick(listOf(3), 42)
            client.pick("minScore=5")
            val (first, second) = CatalogStubs.esRequests()
            val body = first.toString()
            body.contains(""""random_score":{}""") shouldBe true
            body.contains("""{"term":{"tags":"time-travel"}}""") shouldBe true
            body.contains("""{"term":{"genres":"drama"}}""") shouldBe true
            body.contains("""{"range":{"season_year":{"gte":2010}}}""") shouldBe true
            body.contains("""{"term":{"status":"REMOVED"}}""") shouldBe true
            body.contains("""{"term":{"is_adult":false}}""") shouldBe true
            first["size"]!!.jsonPrimitive.content shouldBe "1"
            second.toString().contains("""{"range":{"score":{"gte":5.0}}}""") shouldBe true
        }

    @Test
    fun `adult opt-in always uses Elasticsearch, with the user's exclusions`() =
        api { client ->
            val id = user(showAdult = true)
            TestInfra.sql("INSERT INTO app.user_anime (user_id, anime_id, status) VALUES ('$id', 1, 'watching')")
            CatalogStubs.esHits(listOf(6))
            client.pick("genre=romance", FakeKeycloak.token(sub = id)).ids shouldContainExactly listOf(6L)
            val body = CatalogStubs.esRequests().single().toString()
            body.contains("is_adult") shouldBe false
            body.contains("""{"terms":{"id":[1]}}""") shouldBe true
        }

    @Test
    fun `Elasticsearch being down is a 503`() =
        api { client ->
            CatalogStubs.esDown()
            client.get("/v1/anime/random?tag=school").let {
                it.status shouldBe HttpStatusCode.ServiceUnavailable
                it.problem() shouldBe "search-unavailable"
            }
        }

    @Test
    fun `invalid parameters are 400`() =
        api { client ->
            listOf(
                "count=11",
                "count=0",
                "decade=2015",
                "length=huge",
                "minScore=11",
                "genre=Bad%20Slug",
                "source=other",
                "hideWatched=maybe",
            ).forEach { client.get("/v1/anime/random?$it").problem() shouldBe "validation-failed" }
        }
}
