package picker.features

import io.kotest.matchers.shouldBe
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import picker.support.ApiTest
import picker.support.FakeKeycloak
import picker.support.TestInfra
import picker.support.bearer
import picker.support.json
import java.util.UUID

class MetaTest : ApiTest() {
    private fun names(body: String) =
        body.json().jsonArray.associate {
            it.jsonObject["slug"]!!.jsonPrimitive.content to
                it.jsonObject["name"]!!.jsonPrimitive.content
        }

    @Test
    fun `genres are localized with fallback`() =
        api { client ->
            val response = client.get("/v1/meta/genres?lang=ru")
            response.status shouldBe HttpStatusCode.OK
            response.headers[HttpHeaders.ContentLanguage] shouldBe "ru"
            val ru = names(response.bodyAsText())
            ru["action"] shouldBe "Экшен"
            ru["romance"] shouldBe "Romance" // no Russian name: ru → en
            names(client.get("/v1/meta/genres?lang=ja-Latn").bodyAsText())["drama"] shouldBe "Drama" // ja_latn → en
        }

    @Test
    fun `Accept-Language is used when there is no lang parameter`() =
        api { client ->
            val response =
                client.get(
                    "/v1/meta/genres",
                ) { header(HttpHeaders.AcceptLanguage, "ja-JP,ja;q=0.9,en;q=0.8") }
            response.headers[HttpHeaders.ContentLanguage] shouldBe "ja"
            names(response.bodyAsText())["action"] shouldBe "アクション"
        }

    @Test
    fun `a signed-in user's saved locale applies when nothing else is given`() =
        api { client ->
            val sub = UUID.randomUUID()
            TestInfra.sql("INSERT INTO app.app_user (id, locale) VALUES ('$sub', 'ru')")
            val response = client.get("/v1/meta/genres") { bearer(FakeKeycloak.token(sub = sub)) }
            response.headers[HttpHeaders.ContentLanguage] shouldBe "ru"
        }

    @Test
    fun `unsupported lang is a 400 problem`() =
        api { client ->
            val response = client.get("/v1/meta/genres?lang=de")
            response.status shouldBe HttpStatusCode.BadRequest
            response
                .bodyAsText()
                .json()
                .jsonObject["type"]!!
                .jsonPrimitive.content shouldBe "unsupported-locale"
        }

    @Test
    fun `tags filter by prefix in the request locale`() =
        api { client ->
            val tags =
                client
                    .get("/v1/meta/tags?lang=ru&q=пут")
                    .bodyAsText()
                    .json()
                    .jsonArray
            tags.map { it.jsonObject["slug"]!!.jsonPrimitive.content } shouldBe listOf("time-travel")
            val all =
                client
                    .get("/v1/meta/tags")
                    .bodyAsText()
                    .json()
                    .jsonArray
            all
                .first {
                    it.jsonObject["slug"]!!.jsonPrimitive.content == "tragedy"
                }.jsonObject["spoiler"]!!
                .jsonPrimitive.content shouldBe
                "true"
        }
}
