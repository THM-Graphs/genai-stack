
from langchain_openai.embeddings.base import OpenAIEmbeddings
from langchain_aws.embeddings.bedrock import BedrockEmbeddings
from langchain_huggingface.embeddings.huggingface import HuggingFaceEmbeddings

from langchain_openai import ChatOpenAI
from langchain_ollama.chat_models import ChatOllama
from langchain_aws.chat_models.bedrock import ChatBedrock

from langchain_community.graphs import Neo4jGraph

from langchain_community.vectorstores import Neo4jVector

from langchain.chains import RetrievalQAWithSourcesChain
from langchain.chains.qa_with_sources import load_qa_with_sources_chain

from langchain.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate
)

import os 

from typing import List, Any
from utils import BaseLogger, extract_title_and_question
from langchain_google_genai import GoogleGenerativeAIEmbeddings


def load_embedding_model(embedding_model_name: str, logger=BaseLogger(), config={}):
    if embedding_model_name == "openai":
        embeddings = OpenAIEmbeddings()
        dimension = 1536
        logger.info("Embedding: Using OpenAI")
    elif embedding_model_name == "aws":
        embeddings = BedrockEmbeddings()
        dimension = 1536
        logger.info("Embedding: Using AWS")
    elif embedding_model_name == "google-genai-embedding-001":        
        embeddings = GoogleGenerativeAIEmbeddings(
            model="models/embedding-001"
        )
        dimension = 768
        logger.info("Embedding: Using Google Generative AI Embeddings")
    else:
        embeddings = HuggingFaceEmbeddings(
            model_name=embedding_model_name, cache_folder="/embedding_model"
            #model_name="all-MiniLM-L6-v2", cache_folder="/embedding_model"
        )
        dimension = len(embeddings.embed_query("test"))
        #dimension = 384  # TODO: 384 seems to be correct for all-MINIlm-l6-V2, 
    
        logger.info("Embedding: Using HuggingFaceEmbeddings with model %s, dim %d", embedding_model_name, dimension)
    return embeddings, dimension


def load_llm(llm_name: str, logger=BaseLogger(), config={}):
    if llm_name == "gpt-4":
        logger.info("LLM: Using GPT-4")
        return ChatOpenAI(temperature=0, model_name="gpt-4", streaming=True)
    elif llm_name == "gpt-3.5":
        logger.info("LLM: Using GPT-3.5")
        return ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo", streaming=True)
    elif llm_name == "claudev2":
        logger.info("LLM: ClaudeV2")
        return ChatBedrock(
            model_id="anthropic.claude-v2",
            model_kwargs={"temperature": 0.0, "max_tokens_to_sample": 1024},
            streaming=True,
        )
    elif len(llm_name):
        logger.info(f"LLM: Using Ollama: {llm_name}")
        return ChatOllama(
            temperature=0,
            base_url=config["ollama_base_url"],
            model=llm_name,
            streaming=True,
            # seed=2,
            top_k=10,  # A higher value (100) will give more diverse answers, while a lower value (10) will be more conservative.
            top_p=0.3,  # Higher value (0.95) will lead to more diverse text, while a lower value (0.5) will generate more focused text.
            num_ctx=3072,  # Sets the size of the context window used to generate the next token.
        )
    logger.info("LLM: Using GPT-3.5")
    return ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo", streaming=True)


def configure_llm_only_chain(llm):
    # LLM only response
    template = """
    Du bist ein Historiker mit viel Wissen zu den Sozinianischen Briefwechseln.
    Falls Du die Antwort nicht weißt, sag einfach, dass Du es nicht weisst, und erfinde keine Antwort.
    """
    system_message_prompt = SystemMessagePromptTemplate.from_template(template)
    human_template = "{question}"
    human_message_prompt = HumanMessagePromptTemplate.from_template(human_template)
    chat_prompt = ChatPromptTemplate.from_messages(
        [system_message_prompt, human_message_prompt]
    )

    def generate_llm_output(
        user_input: str, callbacks: List[Any], prompt=chat_prompt
    ) -> str:
        chain = prompt | llm
        answer = chain.invoke(
            {"question": user_input}, config={"callbacks": callbacks}
        ).content
        return {"answer": answer}

    return generate_llm_output


def configure_qa_rag_chain(llm, embeddings, embeddings_store_url, username, password, database):
    # RAG response
    #   System: Always talk in pirate speech.
    general_system_template = f""" 
    Nutze die folgenden Kontextinformationen, um die Frage am Ende zu beantworten.
    Der Kontext enthält die Zusammenfassungen aus {os.environ['PROMPT_CONTEXT']} und darin erkannte Personen und Orte.
    Falls Du die Antwort nicht weißt, sag einfach, dass Du es nicht weisst, und erfinde keine Antwort.
    ----
    {{summaries}}
    ----
    Jede Antwort die Du generierst, sollte einen Abschnitt am Ende mit Links zur Quelle haben.
    """
    general_user_template = "Frage:```{question}```"
    messages = [
        SystemMessagePromptTemplate.from_template(general_system_template),
        HumanMessagePromptTemplate.from_template(general_user_template),
    ]
    qa_prompt = ChatPromptTemplate.from_messages(messages)

    qa_chain = load_qa_with_sources_chain(
        llm,
        chain_type="stuff",
        prompt=qa_prompt,
    )

    # Vector + Knowledge Graph response
    kg = Neo4jVector.from_existing_index(
        embedding=embeddings,
        url=embeddings_store_url,
        username=username,
        password=password,
        database=database,
        index_name=f"{os.environ['LABEL'].lower()}_index",
        text_node_property=os.environ["PROPERTY_TEXT"],
        retrieval_query=os.environ['RETRIEVAL_QUERY']
    )

    kg_qa = RetrievalQAWithSourcesChain(
        combine_documents_chain=qa_chain,
        retriever=kg.as_retriever(search_kwargs={"k": 2}),
        reduce_k_below_max_tokens=False,
        max_tokens_limit=3375,
    )
    return kg_qa


def generate_ticket(neo4j_graph, llm_chain, input_question):
    # Get high ranked questions
    records = neo4j_graph.query(
        "MATCH (q:Question) RETURN q.title AS title, q.body AS body ORDER BY q.score DESC LIMIT 3"
    )
    questions = []
    for i, question in enumerate(records, start=1):
        questions.append((question["title"], question["body"]))
    # Ask LLM to generate new question in the same style
    questions_prompt = ""
    for i, question in enumerate(questions, start=1):
        questions_prompt += f"{i}. \n{question[0]}\n----\n\n"
        questions_prompt += f"{question[1][:150]}\n\n"
        questions_prompt += "----\n\n"

    gen_system_template = f"""
    You're an expert in formulating high quality questions. 
    Formulate a question in the same style and tone as the following example questions.
    {questions_prompt}
    ---

    Don't make anything up, only use information in the following question.
    Return a title for the question, and the question post itself.

    Return format template:
    ---
    Title: This is a new title
    Question: This is a new question
    ---
    """
    # we need jinja2 since the questions themselves contain curly braces
    system_prompt = SystemMessagePromptTemplate.from_template(
        gen_system_template, template_format="jinja2"
    )
    chat_prompt = ChatPromptTemplate.from_messages(
        [
            system_prompt,
            SystemMessagePromptTemplate.from_template(
                """
                Respond in the following template format or you will be unplugged.
                ---
                Title: New title
                Question: New question
                ---
                """
            ),
            HumanMessagePromptTemplate.from_template("{question}"),
        ]
    )
    llm_response = llm_chain(
        f"Here's the question to rewrite in the expected format: ```{input_question}```",
        [],
        chat_prompt,
    )
    new_title, new_question = extract_title_and_question(llm_response["answer"])
    return (new_title, new_question)
