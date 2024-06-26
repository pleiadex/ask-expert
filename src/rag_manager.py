import pprint

# Constants
from constants.preambles import *
from constants.ids import CHUNK_ID

# Models
from models.route_model import WebSearch, Vectorstore
from models.grader_model import GradeDocuments, GradeHallucinations, GradeAnswer
from models.graph_state import GraphState

# Tools
from langchain.schema import Document
from langchain_cohere import ChatCohere
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_community.vectorstores import Chroma
from langchain_community.tools.tavily_search import TavilySearchResults
from langgraph.graph import END, StateGraph
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMChainExtractor
from langchain_cohere import CohereRerank


class RAGManager:

    def __init__(self, chroma_path:str, embedding_function, is_ap:bool, num_docs: int, enable_compression: bool, enable_rerank: bool):
        self.chroma_path = chroma_path
        self.embedding_function = embedding_function
        self.is_ap = is_ap
        self.loop_count = 0
        self.max_loops = 3
        self.num_docs = num_docs
        self.is_compression_enable = enable_compression
        self.is_rerank_enable = enable_rerank

    
    def build_index(self):
        vectorstore = Chroma(
            persist_directory=self.chroma_path, 
            embedding_function=self.embedding_function
        )

        self.retriever = vectorstore.as_retriever(search_kwargs={"k": self.num_docs})
        print(self.num_docs)

    def retrieve(self, state):
        """
        Retrieve documents

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, documents, that contains retrieved documents
        """
        print("---RETRIEVE---")
        question = state["question"]

        self.build_index()
        
        if self.is_rerank_enable:
            print("Rerank")
            compressor = CohereRerank(top_n=self.num_docs)
        else:
            llm = ChatCohere(model="command-r", temperature=0)
            compressor = LLMChainExtractor.from_llm(llm=llm)
            
        # Retrieval
        if self.is_compression_enable:
            compression_retriever = ContextualCompressionRetriever(
                                        base_retriever=self.retriever, 
                                        base_compressor=compressor
                                    )
            retriever = compression_retriever
        else:
            retriever = self.retriever
            
        documents = retriever.invoke(question)
        return {"documents": documents, "question": question}


    def llm_fallback(self, state):
        """
        Generate answer using the LLM w/o vectorstore

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, generation, that contains LLM generation
        """
        print("---LLM Fallback---")
        question = state["question"]

        # LLM
        llm = ChatCohere(model_name="command-r", temperature=0).bind(preamble=LLM_FALLBACK_PREAMBLE if not self.is_ap else LLM_FALLBACK_PREAMBLE_AP)

        # Prompt
        prompt = lambda x: ChatPromptTemplate.from_messages(
            [
                HumanMessage(
                    f"Question: {x['question']} \nAnswer: "
                )
            ]
        )

        # Chain
        llm_chain = prompt | llm | StrOutputParser()

        generation = llm_chain.invoke({"question": question})
        return {"question": question, "generation": generation}        


    def generate(self, state):
        """
        Generate answer using the vectorstore

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, generation, that contains LLM generation
        """
        print("---GENERATE---")
        question = state["question"]
        documents = state["documents"]
        if not isinstance(documents, list):
            documents = [documents]

        # LLM
        llm = ChatCohere(model_name="command-r", temperature=0).bind(preamble=GENERATE_PREAMBLE if not self.is_ap else GENERATE_PREAMBLE_AP)

        # Prompt
        prompt = lambda x: ChatPromptTemplate.from_messages(
            [
                HumanMessage(
                    f"Question: {x['question']} \nAnswer: ",
                    additional_kwargs={"documents": x["documents"]},
                )
            ]
        )

        # Chain
        rag_chain = prompt | llm | StrOutputParser()

        # RAG generation
        generation = rag_chain.invoke({"documents": documents, "question": question})
        return {"documents": documents, "question": question, "generation": generation}



    def grade_documents(self, state):
        """
        Determines whether the retrieved documents are relevant to the question.

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Updates documents key with only filtered relevant documents
        """

        print("---CHECK DOCUMENT RELEVANCE TO QUESTION---")
        question = state["question"]
        documents = state["documents"]

        # LLM with function call
        llm = ChatCohere(model="command-r", temperature=0)
        structured_llm_grader = llm.with_structured_output(GradeDocuments, preamble=DOCUMENT_GRADER_PREAMBLE)

        grade_prompt = ChatPromptTemplate.from_messages(
            [
                ("human", "Retrieved document: \n\n {document} \n\n User question: {question}"),
            ]
        )

        retrieval_grader = grade_prompt | structured_llm_grader

        # Score each doc
        filtered_docs = []
        for d in documents:
            score = retrieval_grader.invoke({"question": question, "document": d.page_content})
            grade = score.binary_score
            if grade == YES:
                print("---GRADE: DOCUMENT RELEVANT---")
                filtered_docs.append(d)
            else:
                print("---GRADE: DOCUMENT NOT RELEVANT---")
                continue
        return {"documents": filtered_docs, "question": question}


    def web_search(self, state):
        """
        Web search based on the re-phrased question.

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Updates documents key with appended web results
        """

        print("---WEB SEARCH---")
        question = state["question"]

        # Web search
        web_search_tool = TavilySearchResults()
        docs = web_search_tool.invoke({"query": question})

        # TODO: handle unsuccessful search
        try:
            web_results = "\n".join([d["content"] for d in docs])
        except:
            web_results = "The question is too long."

        web_results = Document(page_content=web_results)

        return {"documents": web_results, "question": question}



    def route_question(self, state):
        """
        Route question to web search or RAG.

        Args:
            state (dict): The current graph state

        Returns:
            str: Next node to call
        """

        print("---ROUTE QUESTION---")
        question = state["question"]

        # LLM with tool use and preamble
        llm = ChatCohere(model="command-r", temperature=0)
        
        structured_llm_router = llm.bind_tools(tools=[WebSearch, Vectorstore], preamble=ROUTE_QUESTION_PREAMBLE_WITH_WEB_SEARCH)
            
        # Prompt
        route_prompt = ChatPromptTemplate.from_messages(
            [
                ("human", "{question}"),
            ]
        )

        question_router = route_prompt | structured_llm_router

        source = question_router.invoke({"question": question})
        
        # Fallback to LLM or raise error if no decision
        if "tool_calls" not in source.additional_kwargs:
            print("---ROUTE QUESTION TO LLM---")
            return "llm_fallback" 
        if len(source.additional_kwargs["tool_calls"]) == 0:
            raise "Router could not decide source"

        # Choose datasource
        datasource = source.additional_kwargs["tool_calls"][0]["function"]["name"]
        if datasource == 'web_search':
            print("---ROUTE QUESTION TO WEB SEARCH---")
            return "web_search"
        elif datasource == 'vectorstore':
            print("---ROUTE QUESTION TO RAG---")
            return "vectorstore"
        else: 
            print("---ROUTE QUESTION TO LLM---")
            return "vectorstore"


    def decide_to_generate(self, state):
        """
        Determines whether to generate an answer, or re-generate a question.

        Args:
            state (dict): The current graph state

        Returns:
            str: Binary decision for next node to call
        """

        print("---ASSESS GRADED DOCUMENTS---")
        question = state["question"]
        filtered_documents = state["documents"]

        if not filtered_documents:
            # All documents have been filtered check_relevance
            # We will re-generate a new query

            print("---DECISION: ALL DOCUMENTS ARE NOT RELEVANT TO QUESTION, WEB SEARCH---")
            return "web_search"
        else:
            # We have relevant documents, so generate answer
            print("---DECISION: GENERATE---")
            return "generate"


    def grade_generation_v_documents_and_question(self, state):
        """
        Determines whether the generation is grounded in the document and answers question.

        Args:
            state (dict): The current graph state

        Returns:
            str: Decision for next node to call
        """

        print("---CHECK HALLUCINATIONS---")
        question = state["question"]
        documents = state["documents"]
        generation = state["generation"]

        # LLM with function call
        llm = ChatCohere(model="command-r", temperature=0)
        structured_llm_grader = llm.with_structured_output(GradeHallucinations, preamble=HALLUCINATION_GRADER_PREAMBLE)

        # Prompt
        hallucination_prompt = ChatPromptTemplate.from_messages(
            [
                # ("system", system),
                ("human", "Set of facts: \n\n {documents} \n\n LLM generation: {generation}"),
            ]
        )

        hallucination_grader = hallucination_prompt | structured_llm_grader

        # LLM with function call
        llm = ChatCohere(model="command-r", temperature=0)
        structured_llm_grader = llm.with_structured_output(GradeAnswer, preamble=ANSWER_GRADER_PREMABLE)

        # Prompt
        answer_prompt = ChatPromptTemplate.from_messages(
            [
                ("human", "User question: \n\n {question} \n\n LLM generation: {generation}"),
            ]
        )

        answer_grader = answer_prompt | structured_llm_grader
        score = hallucination_grader.invoke({"documents": documents, "generation": generation})
        grade = score.binary_score

        self.loop_count += 1

        # avoid infinite loop
        if self.loop_count == self.max_loops:
            return "useful"
        
        # Check hallucination
        if grade == YES:
            print("---DECISION: GENERATION IS GROUNDED IN DOCUMENTS---")
            # Check question-answering
            print("---GRADE GENERATION vs QUESTION---")
            score = answer_grader.invoke({"question": question,"generation": generation})

            try: 
                grade = score.binary_score
            except:
                grade = NO

            if grade == YES:
                print("---DECISION: GENERATION ADDRESSES QUESTION---")
                return "useful"
            else:
                print("---DECISION: GENERATION DOES NOT ADDRESS QUESTION---")                
                return "not useful"
        else:
            print("---DECISION: GENERATION IS NOT GROUNDED IN DOCUMENTS, RE-TRY---")
            return "not supported"


    def build_graph(self):
        workflow = StateGraph(GraphState)

        # Define the nodes
        workflow.add_node("web_search", self.web_search) # web search
        workflow.add_node("retrieve", self.retrieve) # retrieve
        workflow.add_node("grade_documents", self.grade_documents) # grade documents
        workflow.add_node("generate", self.generate) # rag
        workflow.add_node("llm_fallback", self.llm_fallback) # llm

        # Build graph
        workflow.set_conditional_entry_point(
            self.route_question,
            {
                "web_search": "web_search",
                "vectorstore": "retrieve",
                "llm_fallback": "llm_fallback",
            },
        )
        workflow.add_edge("web_search", "generate")
        workflow.add_edge("retrieve", "grade_documents")
        workflow.add_conditional_edges(
            "grade_documents",
            self.decide_to_generate,
            {
                "web_search": "web_search",
                "generate": "generate",
            },
        )
        workflow.add_conditional_edges(
            "generate",
            self.grade_generation_v_documents_and_question,
            {
                "not supported": "generate", # Hallucinations: re-generate 
                "not useful": "web_search", # Fails to answer question: fall-back to web-search 
                "useful": END,
            },
        )
        workflow.add_edge("llm_fallback", END)

        # Compile
        app = workflow.compile()

        return app


    def run(self, question:str):
        
        # Compile the StateGraph application
        app = self.build_graph()
        
        # The input to the application will be the given prompt
        inputs = {"question": question}
        
        # Print state information as the app traverses each node
        for output in app.stream(inputs):
            for key, value in output.items():
                pprint.pprint(f"Node '{key}':")
            pprint.pprint("\n---\n")

        # Print final response, or the generated text
        response = value["generation"]
        pprint.pprint(response)

        # add refereces to response
        sources = None
        contexts = None

        if "documents" in value:
            sources = [doc.metadata.get(CHUNK_ID, None) for doc in value["documents"]]
            contexts = [doc.page_content for doc in value["documents"]]
        
        return response, sources, contexts


    @staticmethod
    def get_answer_wo_rag(question: str, is_ap=False):

        prompt_template = ChatPromptTemplate(
            messages=[
                HumanMessage(
                    f"""{PURE_LLM_PREAMBLE if not is_ap else PURE_LLM_PREAMBLE_AP}\n\nQuestion: {question}"""
                )
            ]
        
        )
        prompt = prompt_template.format(question=question)

        llm = ChatCohere(model="command-r", temperature=0)

        chain = (
            llm |
            StrOutputParser()
        )

        response = chain.invoke(prompt)

        return response
    
    def get_answer_w_vanilla_rag(self, question: str):

        db = Chroma(
            persist_directory=self.chroma_path, 
            embedding_function=self.embedding_function
        )

        results = db.similarity_search_with_score(question, k=self.num_docs)

        # Prompt
        prompt = lambda x: ChatPromptTemplate.from_messages(
            [
                HumanMessage(
                    f"Question: {x['question']} \nAnswer: ",
                    additional_kwargs={"documents": x},
                )
            ]
        )

        llm = ChatCohere(model_name="command-r", temperature=0).bind(preamble=GENERATE_PREAMBLE if not self.is_ap else GENERATE_PREAMBLE_AP)

        chain = (
            prompt |
            llm |
            StrOutputParser()
        )

        response = chain.invoke({"documents": results, "question": question})

        sources = [f'{doc.metadata.get("id", None)}:{_score}' for doc, _score in results]

        contexts = [doc.page_content for doc, _ in results]


        return response, sources, contexts
