import logging
import os
import sys

import chainlit as cl
from llama_index import StorageContext
from llama_index.callbacks import CallbackManager
from llama_index.prompts import PromptTemplate
from llama_index.prompts.base import PromptType
from llama_index.query_engine import CitationQueryEngine

from citation import add_graph, get_formatted_sources
from graph_stores import CustomNeo4jGraphStore
from query_engine import CustomCitationQueryEngine
from retrievers import KG_RAG_KnowledgeGraphRAGRetriever
from service_context import get_service_context
from translation import detect_language, translate

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logging.getLogger().addHandler(logging.StreamHandler(stream=sys.stdout))


@cl.on_chat_start
async def factory():
    callback_manager = CallbackManager([cl.LlamaIndexCallbackHandler()])
    service_context = get_service_context(callback_manager=callback_manager)

    graph_store = CustomNeo4jGraphStore(
        username="neo4j",
        password=os.environ["NEO4J_PASSWORD"],
        url="bolt://neo4j:7687",
        database="neo4j",
        node_label="Congenital and Genetic Diseases",
    )

    storage_context = StorageContext.from_defaults(
        graph_store=graph_store,
    )

    CUSTOM_QUERY_KEYWORD_EXTRACT_TEMPLATE_TMPL = (
        "A question is provided below. Given the question, extract up to {max_keywords} "
        "diseases from the text. Focus on extracting the diseases that we can use "
        "to best lookup answers to the question. Avoid stopwords.\n"
        "---------------------\n"
        "{question}\n"
        "---------------------\n"
        "Provide diseases in the following comma-separated format: 'KEYWORDS: <diseases>'\n"
    )

    retriever = KG_RAG_KnowledgeGraphRAGRetriever(
        storage_context=storage_context,
        verbose=True,
        service_context=service_context,
        graph_traversal_depth=1,
        max_entities=3,
        max_synonyms=1,
        similarity_top_k=100,
        entity_extract_template=PromptTemplate(
            CUSTOM_QUERY_KEYWORD_EXTRACT_TEMPLATE_TMPL,
            prompt_type=PromptType.QUERY_KEYWORD_EXTRACT,
        ),
    )

    CUSTOM_CITATION_QA_TEMPLATE = PromptTemplate(
        "Please provide an answer based solely on the provided sources. "
        "When referencing information from a source, "
        "cite the appropriate source(s) using their corresponding numbers. "
        "Every answer should include at least one source citation. "
        "Only cite a source when you are explicitly referencing it. "
        "If none of the sources are helpful, you should indicate that. "
        "Once you have answered the question, stop and say 'DONE'."
        "For example:\n"
        "Source 1:\n"
        "The sky is red in the evening and blue in the morning.\n"
        "Source 2:\n"
        "Water is wet when the sky is red.\n"
        "Query: When is water wet?\n"
        "Answer: Water will be wet when the sky is red (SOURCE 2), "
        "which occurs in the evening (SOURCE 1).\n"
        "DONE\n"
        "Now it's your turn. Below are several numbered sources of information:"
        "\n------\n"
        "{context_str}"
        "\n------\n"
        "Query: {query_str}\n"
        "Answer: "
    )

    CUSTOM_CITATION_REFINE_TEMPLATE = PromptTemplate(
        "Please provide an answer based solely on the provided sources. "
        "When referencing information from a source, "
        "cite the appropriate source(s) using their corresponding numbers. "
        "Every answer should include at least one source citation. "
        "Only cite a source when you are explicitly referencing it. "
        "If none of the sources are helpful, you should indicate that. "
        "Once you have answered the question, stop and say 'DONE'."
        "For example:\n"
        "Source 1:\n"
        "The sky is red in the evening and blue in the morning.\n"
        "Source 2:\n"
        "Water is wet when the sky is red.\n"
        "Query: When is water wet?\n"
        "Answer: Water will be wet when the sky is red [(SOURCE 2)], "
        "which occurs in the evening (SOURCE 1).\n"
        "DONE\n"
        "Now it's your turn. "
        "We have provided an existing answer: {existing_answer}"
        "Below are several numbered sources of information. "
        "Use them to refine the existing answer. "
        "If the provided sources are not helpful, you will repeat the existing answer."
        "\nBegin refining!"
        "\n------\n"
        "{context_msg}"
        "\n------\n"
        "Query: {query_str}\n"
        "Answer: "
    )

    query_engine = CustomCitationQueryEngine.from_args(
        service_context,
        retriever=retriever,
        citation_qa_template=CUSTOM_CITATION_QA_TEMPLATE,
        citation_refine_template=CUSTOM_CITATION_REFINE_TEMPLATE,
        use_async=True,
        streaming=True,
        verbose=True,
    )
    cl.user_session.set("query_engine", query_engine)


@cl.on_message
async def main(message: cl.Message):
    query_engine: CitationQueryEngine = cl.user_session.get("query_engine")
    content = message.content

    detected_language = await detect_language(content)
    if detected_language != "en" and detected_language is not None:
        content = await translate(content, target="en")

    response = await cl.make_async(query_engine.query)(content)
    response_message =  cl.Message(content="")

    if hasattr(response, "response_gen"):
        for token in response.response_gen:
            await response_message.stream_token(token=token)

    content = response_message.content
    response_message.content = response_message.content.removesuffix("DONE")

    if detected_language != "en" and detected_language is not None:
        response_message.content = await translate(response_message.content, target=detected_language)

    response_message.content += await get_formatted_sources(response, content)

    add_graph(response_message)

    await response_message.send()
