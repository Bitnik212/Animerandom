ARG ES_VERSION=8.19.4
FROM docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}
RUN bin/elasticsearch-plugin install --batch analysis-kuromoji \
 && bin/elasticsearch-plugin install --batch analysis-icu
