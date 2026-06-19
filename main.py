# -*- coding: utf-8 -*-
import sys
import os
import json
import re
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QLineEdit, QTextEdit, QTabWidget, 
                             QFileDialog, QMessageBox, QComboBox, QProgressBar, QGroupBox,
                             QScrollArea, QGridLayout, QFormLayout, QSizePolicy, QCheckBox,
                             QListWidget, QSplitter)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette
from config_manager import ConfigManager
from llm_client import LLMClient
from file_parser import FileParser
from data_auditor import DataAuditor
from exporter import Exporter
from web_scraper import WebScraper, PaperDataExtractor, SmartDataExtractor, DeclarationDataExtractor
from logger import get_logger


# 初始化日志
logger = get_logger()
logger.info("=" * 50)
logger.info("程序启动")


class FileParseThread(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(str, str)
    error = pyqtSignal(str)
    
    def __init__(self, file_path, file_type, config_manager):
        super().__init__()
        self.file_path = file_path
        self.file_type = file_type
        self.config_manager = config_manager
    
    def run(self):
        try:
            logger.info(f"开始解析文件: {self.file_path}, 类型: {self.file_type}")
            
            # 设置进度回调
            def progress_callback(current, total):
                percentage = int((current / total) * 100)
                self.progress.emit(percentage, f"正在解析第 {current}/{total} 页...")
            
            FileParser.set_progress_callback(progress_callback)
            
            self.progress.emit(5, "正在打开文件...")
            
            # 解析文件
            text = FileParser.parse_file(self.file_path, self.config_manager)
            
            logger.info(f"文件解析完成，文本长度: {len(text)} 字符")
            self.progress.emit(100, "解析完成！")
            self.finished.emit(text, self.file_type)
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            logger.error(f"文件解析失败: {str(e)}", exc_info=True)
            self.error.emit(f"{str(e)}\n{error_detail}")


class PaperDataExtractThread(QThread):
    """专门用于从论文/申报书中提取结构化量化数据的线程（智能识别文档类型）"""
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(list, str)  # 返回提取的数据和文件类型
    error = pyqtSignal(str)
    
    def __init__(self, paper_text, file_type, model_type, api_key, file_name=""):
        super().__init__()
        self.paper_text = paper_text
        self.file_type = file_type
        self.model_type = model_type
        self.api_key = api_key
        self.file_name = file_name
    
    def run(self):
        try:
            logger.info(f"开始文档数据提取，文本长度: {len(self.paper_text)}, 模型: {self.model_type}, 文件名: {self.file_name}")
            
            # 步骤1：智能识别文档类型并准备提取任务
            self.progress.emit(10, "正在智能识别文档类型...")
            tasks = SmartDataExtractor.prepare_tasks(self.paper_text, self.file_name)
            total_tasks = len(tasks)
            
            # 从第一个任务获取文档类型
            doc_type = tasks[0].get('doc_type', 'paper') if tasks else 'paper'
            doc_type_label = "申报书" if doc_type == "declaration" else "论文"
            logger.info(f"提取任务准备完成，文档类型: {doc_type_label}, 任务数: {total_tasks}")
            
            # 步骤2：执行任务
            responses = []
            for idx, task in enumerate(tasks):
                progress_val = int(10 + (idx / total_tasks) * 70)
                chunk_idx = task.get('chunk_idx', 1)
                total_chunks = task.get('total_chunks', 1)
                
                if total_chunks > 1:
                    status_msg = f"正在处理{doc_type_label}第{chunk_idx}/{total_chunks}段 ({idx+1}/{total_tasks})"
                else:
                    status_msg = f"正在从{doc_type_label}中提取数据 ({idx+1}/{total_tasks})"
                
                self.progress.emit(progress_val, status_msg)
                
                try:
                    llm = LLMClient(self.model_type, self.api_key)
                    response = llm.call_llm(task.get('prompt', ''))
                    logger.debug(f"任务{idx+1}完成，响应长度: {len(response) if response else 0}")
                    
                    responses.append({
                        'response': response
                    })
                except Exception as e:
                    logger.error(f"任务{idx+1}调用失败: {str(e)}", exc_info=True)
                    responses.append({
                        'response': f"提取失败: {str(e)}"
                    })
            
            # 步骤3：处理响应
            self.progress.emit(85, "正在解析提取结果...")
            extracted_data = SmartDataExtractor.process_responses(responses, doc_type)
            
            logger.info(f"数据提取完成，提取到 {len(extracted_data)} 条数据")
            self.progress.emit(100, f"成功从{doc_type_label}中提取{len(extracted_data)}条数据！")
            self.finished.emit(extracted_data, self.file_type)
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            logger.error(f"论文数据提取失败: {str(e)}", exc_info=True)
            self.error.emit(f"数据提取失败: {str(e)}\n{error_detail}")


class GenerateThread(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, model_type, api_key, title, project_text, reference_text, project_info=None, extracted_project_data=None, extracted_reference_data=None):
        super().__init__()
        self.model_type = model_type
        self.api_key = api_key
        self.title = title
        self.project_text = project_text
        self.reference_text = reference_text
        self.project_info = project_info
        self.extracted_project_data = extracted_project_data if extracted_project_data else []
        self.extracted_reference_data = extracted_reference_data if extracted_reference_data else []

    def run(self):
        try:
            self.progress.emit(5, "正在初始化...")
            
            content_to_analyze = ""
            
            if self.title:
                content_to_analyze += f"项目标题: {self.title}\n"
            
            if self.project_text:
                self.progress.emit(25, "正在深度理解项目思路...")
                scraper = WebScraper()
                
                if not self.project_info:
                    self.project_info = scraper.extract_project_info(self.project_text)
                
                content_to_analyze += f"\n[项目思路上下文]\n{self.project_text[:10000]}\n"
            
            # 初始化最终结果 - 6个维度
            final_result = {
                "项目背景领域现状量化数据": [],
                "目标用户详细分析数据": [],
                "相关政策支撑数据": [],
                "痛点与行业缺口量化数据": [],
                "项目竞争力对比数据": [],
                "项目创新点支撑数据": []
            }
            
            # 处理已提取的参考文献数据，按 category 分配到对应维度
            if self.extracted_reference_data:
                self.progress.emit(40, "正在整理参考文献数据...")
                self._distribute_extracted_data(final_result, self.extracted_reference_data)
            
            # 调用大模型补充分析
            if self.reference_text or self.project_text:
                self.progress.emit(65, "正在执行文献-项目交叉分析...")
                try:
                    llm = LLMClient(self.model_type, self.api_key)
                    prompt = self._build_prompt(content_to_analyze, self.project_info)
                    result = llm.call_llm(prompt)
                    raw_data_dict = self._parse_llm_result(result)
                    
                    # 将大模型返回的数据合并到对应维度
                    self._merge_llm_data(final_result, raw_data_dict)
                except Exception as e:
                    logger.warning(f"LLM调用失败，仅使用已提取的数据: {e}")
            
            # 对每个维度的数据进行审核
            self.progress.emit(85, "正在审核数据...")
            statistician = DataAuditor(self.project_info)
            
            for key in final_result:
                if final_result[key]:
                    final_result[key] = statistician.audit_data(final_result[key])
            
            # 移除空的维度
            final_result = {k: v for k, v in final_result.items() if v}
            
            self.progress.emit(100, "分析完成！")
            self.finished.emit(final_result)
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            self.error.emit(f"{str(e)}\n{error_detail}")

    def _build_prompt(self, web_content, project_info):
        prompt = f"""=== 大创项目文献政策深度分析工具 ===

【核心任务】
您的任务是深入理解和分析用户导入的文献、政策、论文，从中提取与项目思路高度相关的统计数据。
重点是：深度理解项目思路，从导入的文献中筛选、整理、统计与项目高度契合的内容，不编造任何内容！

【强制规则】
1. 所有输出内容必须100%来自用户导入的参考文献、政策文档，不得编造数据
2. 所有数据必须标注明确来源（来自哪份导入的文档），100%可追溯
3. 如果导入内容中某项数据不足，输出空数组即可，绝不编造
4. 重点统计与项目主题、研究问题、创新点、技术路线高度相关的内容
5. 项目申报书仅用于理解项目思路，不作为输出数据的来源
6. 深度分析文献内容，充分挖掘与项目相关的信息

【分析重点】
请重点从导入文献中统计以下6类与项目相关的数据：
1. 项目背景领域现状量化数据：从导入文献中提取领域规模、增速、研究热度、发展水平等
2. 目标用户详细分析数据：从导入文献中提取用户画像、行为、需求等相关数据
3. 相关政策支撑数据：从导入文献中提取相关政策、要求、支持方向等
4. 痛点与行业缺口量化数据：从导入文献中提取现存问题、服务不足、市场空白等
5. 项目竞争力对比数据：从导入文献中提取同类产品对比、竞争格局等
6. 项目创新点支撑数据：从导入文献中提取研究空白、创新方向佐证等

【项目思路理解（用于深度筛选相关文献内容）】
项目标题：{self.title}
"""
        
        if project_info:
            prompt += f"""
项目主题: {project_info.get('theme', '')}
研究方向: {project_info.get('direction', '')}
目标用户: {project_info.get('target_users', '')}
研究问题: {project_info.get('research_questions', '')}
创新点: {project_info.get('innovations', '')}
技术路线: {project_info.get('technical_approach', '')}
研究内容: {project_info.get('research_content', '')}
关键技术: {', '.join(project_info.get('key_technologies', []))}
"""
        
        if self.reference_text:
            prompt += f"""
【参考文献（深度分析）】
{self.reference_text[:15000]}
"""
        
        if web_content:
            prompt += f"""
【导入的其他文献/政策内容】
{web_content}
"""
        
        prompt += """
【输出格式要求】
请严格按照以下JSON格式输出，不要任何其他文字：

{
    "项目背景领域现状量化数据": [
        {
            "数据": "从导入文献中提取的量化数据",
            "来源": "参考文献",
            "source_type": "user_document",
            "publish_date": "2024-01-01"
        }
    ],
    "目标用户详细分析数据": [
        {
            "数据": "从导入文献中提取的用户数据",
            "来源": "参考文献",
            "source_type": "user_document"
        }
    ],
    "相关政策支撑数据": [
        {
            "数据": "从导入文献中提取的政策内容",
            "来源": "参考文献",
            "source_type": "user_document"
        }
    ],
    "痛点与行业缺口量化数据": [
        {
            "数据": "从导入文献中提取的痛点数据",
            "来源": "参考文献",
            "source_type": "user_document"
        }
    ],
    "项目竞争力对比数据": [
        {
            "数据": "从导入文献中提取的竞争数据",
            "来源": "参考文献",
            "source_type": "user_document"
        }
    ],
    "项目创新点支撑数据": [
        {
            "数据": "从导入文献中提取的创新佐证",
            "来源": "参考文献",
            "source_type": "user_document"
        }
    ]
}
"""
        return prompt

    def _distribute_extracted_data(self, final_result, extracted_data):
        """将提取的数据按 category 分配到对应维度"""
        # category 到维度的映射
        category_map = {
            '领域现状': '项目背景领域现状量化数据',
            '用户画像': '目标用户详细分析数据',
            '政策支撑': '相关政策支撑数据',
            '痛点问题': '痛点与行业缺口量化数据',
            '创新点支撑': '项目创新点支撑数据'
        }
        
        for item in extracted_data:
            # 统一数据格式
            formatted_item = self._format_data_item(item)
            
            # 根据 category 分配到对应维度
            category = item.get('category', '')
            target_key = category_map.get(category)
            
            if target_key:
                final_result[target_key].append(formatted_item)
            else:
                # 默认分配到领域现状
                final_result["项目背景领域现状量化数据"].append(formatted_item)
    
    def _merge_llm_data(self, final_result, llm_data_dict):
        """将大模型返回的数据合并到对应维度"""
        for key, data_list in llm_data_dict.items():
            if key in final_result and isinstance(data_list, list):
                for item in data_list:
                    if isinstance(item, dict):
                        formatted_item = self._format_data_item(item)
                        final_result[key].append(formatted_item)
    
    def _format_data_item(self, item):
        """统一数据格式，兼容审核系统"""
        formatted = {
            'source_type': 'user_document',
            'source': item.get('来源', item.get('source', '参考文献'))
        }
        
        # 处理数据字段
        if '数据' in item:
            formatted['data'] = item['数据']
            formatted['content'] = item['数据']
        elif 'content' in item:
            formatted['data'] = item['content']
            formatted['content'] = item['content']
        
        # 处理发布日期
        if 'publish_date' in item:
            formatted['publish_date'] = item['publish_date']
        
        return formatted
    
    def _parse_llm_result(self, result):
        try:
            result_str = str(result)
            start = result_str.find('{')
            end = result_str.rfind('}')
            
            if start != -1 and end != -1:
                json_str = result_str[start:end+1]
                parsed = json.loads(json_str)
                # 过滤掉系统生成的占位数据
                return self._filter_placeholder_data(parsed)
            else:
                return json.loads(result_str)
        except Exception as e:
            logger.warning(f"JSON解析失败: {e}")
            # 返回空字典而不是占位数据
            return {}
    
    def _filter_placeholder_data(self, data_dict):
        """过滤掉系统生成的占位数据"""
        filtered = {}
        for key, data_list in data_dict.items():
            if isinstance(data_list, list):
                valid_data = []
                for item in data_list:
                    if isinstance(item, dict):
                        data_content = item.get('数据', item.get('content', ''))
                        # 排除占位数据
                        if '正在获取' not in data_content and '系统生成' not in str(item.get('来源', '')):
                            valid_data.append(item)
                if valid_data:
                    filtered[key] = valid_data
        return filtered


class ModernButton(QPushButton):
    def __init__(self, text, color="#4A90E2", hover_color="#357ABD"):
        super().__init__(text)
        self.color = color
        self.hover_color = hover_color
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 8px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
            QPushButton:pressed {{
                background-color: #2A5A8E;
            }}
        """)
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.current_data = {}
        self.project_text = ""
        self.reference_text = ""
        self.project_info = None
        self.web_scraper = WebScraper()
        self.init_ui()
        self.apply_theme()
        self.load_config()

    def apply_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#F5F7FA"))
        palette.setColor(QPalette.WindowText, QColor("#2C3E50"))
        palette.setColor(QPalette.Base, QColor("#FFFFFF"))
        palette.setColor(QPalette.AlternateBase, QColor("#E8ECF1"))
        palette.setColor(QPalette.ToolTipBase, QColor("#FFFFFF"))
        palette.setColor(QPalette.ToolTipText, QColor("#2C3E50"))
        palette.setColor(QPalette.Text, QColor("#2C3E50"))
        palette.setColor(QPalette.Button, QColor("#4A90E2"))
        palette.setColor(QPalette.ButtonText, QColor("#FFFFFF"))
        palette.setColor(QPalette.BrightText, QColor("#E74C3C"))
        palette.setColor(QPalette.Link, QColor("#3498DB"))
        palette.setColor(QPalette.Highlight, QColor("#4A90E2"))
        palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        QApplication.setPalette(palette)

    def init_ui(self):
        self.setWindowTitle("大创项目文献政策统计分析工具")
        self.setGeometry(100, 100, 1450, 980)
        self.setMinimumSize(1250, 880)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(30, 30, 30, 30)

        title_label = QLabel("大创项目文献政策统计分析工具")
        title_label.setFont(QFont("Microsoft YaHei UI", 25, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("color: #2C3E50; padding: 10px;")
        main_layout.addWidget(title_label)

        subtitle_label = QLabel("导入文献/政策 → 深度分析项目申报书 → 统计项目相关数据 → 四维度评分排序 → 导出结果")
        subtitle_label.setFont(QFont("Microsoft YaHei UI", 13))
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setStyleSheet("color: #7F8C8D; margin-bottom: 10px;")
        main_layout.addWidget(subtitle_label)

        tab_widget = QTabWidget()
        tab_widget.setFont(QFont("Microsoft YaHei UI", 11))
        tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #DDDDDD;
                border-radius: 10px;
                background: white;
            }
            QTabBar::tab {
                background: #E8ECF1;
                color: #555555;
                padding: 12px 28px;
                margin-right: 5px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                font-weight: bold;
            }
            QTabBar::tab:selected {
                background: #4A90E2;
                color: white;
            }
            QTabBar::tab:hover:!selected {
                background: #D0D8E0;
            }
        """)
        tab_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        self.setup_config_tab(tab_widget)
        self.setup_main_tab(tab_widget)
        self.setup_result_tab(tab_widget)
        self.setup_log_tab(tab_widget)
        main_layout.addWidget(tab_widget)

    def setup_config_tab(self, tab_widget):
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QScrollArea.NoFrame)
        
        config_tab = QWidget()
        layout = QVBoxLayout(config_tab)
        layout.setSpacing(20)
        layout.setContentsMargins(40, 40, 40, 40)

        model_group = QGroupBox("API Key配置")
        model_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        model_layout = QFormLayout(model_group)
        model_layout.setSpacing(18)
        model_layout.setContentsMargins(30, 30, 30, 30)

        self.model_combo = QComboBox()
        self.model_combo.addItems(["OpenRouter (Qwen 3.6 Plus)", "豆包 (Doubao)", "通义千问 (Tongyi)"])
        self.model_combo.setStyleSheet("""
            QComboBox {
                padding: 12px 18px;
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                font-size: 14px;
                min-height: 45px;
            }
        """)
        model_label1 = QLabel("选择模型:")
        model_label1.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        model_label1.setStyleSheet("color: #4A90E2; padding: 8px 0;")
        model_layout.addRow(model_label1, self.model_combo)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("请输入您的API Key（将加密保存在本地，不上传任何服务器）")
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setStyleSheet("""
            QLineEdit {
                padding: 12px 18px;
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                font-size: 14px;
                min-height: 45px;
            }
        """)
        model_label2 = QLabel("API Key:")
        model_label2.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        model_label2.setStyleSheet("color: #4A90E2; padding: 8px 0;")
        model_layout.addRow(model_label2, self.api_key_edit)

        save_btn = ModernButton("保存配置", "#27AE60", "#229954")
        save_btn.clicked.connect(self.save_config)
        model_layout.addRow(save_btn)

        layout.addWidget(model_group)
        
        # 百度OCR配置
        ocr_group = QGroupBox("百度OCR配置（用于PDF解析）")
        ocr_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        ocr_layout = QFormLayout(ocr_group)
        ocr_layout.setSpacing(18)
        ocr_layout.setContentsMargins(30, 30, 30, 30)
        
        self.baidu_api_key_edit = QLineEdit()
        self.baidu_api_key_edit.setPlaceholderText("请输入百度智能云API Key（将加密保存在本地）")
        self.baidu_api_key_edit.setEchoMode(QLineEdit.Password)
        self.baidu_api_key_edit.setStyleSheet("""
            QLineEdit {
                padding: 12px 18px;
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                font-size: 14px;
                min-height: 45px;
            }
        """)
        ocr_label1 = QLabel("API Key:")
        ocr_label1.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        ocr_label1.setStyleSheet("color: #E67E22; padding: 8px 0;")
        ocr_layout.addRow(ocr_label1, self.baidu_api_key_edit)
        
        self.baidu_secret_key_edit = QLineEdit()
        self.baidu_secret_key_edit.setPlaceholderText("请输入百度智能云Secret Key（将加密保存在本地）")
        self.baidu_secret_key_edit.setEchoMode(QLineEdit.Password)
        self.baidu_secret_key_edit.setStyleSheet("""
            QLineEdit {
                padding: 12px 18px;
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                font-size: 14px;
                min-height: 45px;
            }
        """)
        ocr_label2 = QLabel("Secret Key:")
        ocr_label2.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        ocr_label2.setStyleSheet("color: #E67E22; padding: 8px 0;")
        ocr_layout.addRow(ocr_label2, self.baidu_secret_key_edit)
        
        ocr_save_btn = ModernButton("保存OCR配置", "#E67E22", "#D35400")
        ocr_save_btn.clicked.connect(self.save_ocr_config)
        ocr_layout.addRow(ocr_save_btn)
        
        # 添加说明
        ocr_note = QLabel("💡 提示：前往百度智能云开通「文字识别OCR」服务，创建应用获取API Key和Secret Key")
        ocr_note.setWordWrap(True)
        ocr_note.setStyleSheet("""
            QLabel {
                color: #7F8C8D; 
                margin-top: 12px; 
                font-size: 13px;
                background: #F8F9FA;
                padding: 10px 15px;
                border-radius: 8px;
            }
        """)
        ocr_layout.addRow(ocr_note)
        
        layout.addWidget(ocr_group)

        rules_group = QGroupBox("【强制规则说明 - 写死程序逻辑，不可突破】")
        rules_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        rules_layout = QVBoxLayout(rules_group)
        rules_layout.setContentsMargins(30, 30, 30, 30)
        
        rules_text = QTextEdit()
        rules_text.setReadOnly(True)
        rules_text.setMinimumHeight(280)
        rules_text.setStyleSheet("""
            QTextEdit {
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                padding: 15px;
                font-size: 14px;
                line-height: 1.8;
            }
        """)
        rules_text.setHtml("""
        <div style='line-height: 2.2; color: #2C3E50;'>
            <div style='background: linear-gradient(135deg, #FFF5F5 0%, #FFF 100%); padding: 20px; border-radius: 12px; border-left: 5px solid #E74C3C; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #E74C3C; margin-bottom: 8px;'>📌 1. 文献统计分析</div>
                <div style='font-size: 14px;'>所有输出均来自您导入的文献、政策和论文内容，大模型仅负责统计分析，不编造任何无来源数据</div>
            </div>
            <div style='background: linear-gradient(135deg, #F0F7FF 0%, #FFF 100%); padding: 20px; border-radius: 12px; border-left: 5px solid #3498DB; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #3498DB; margin-bottom: 8px;'>📌 2. 项目相关性</div>
                <div style='font-size: 14px;'>重点统计与项目申报书主题、研究问题、创新点高度相关的文献内容</div>
            </div>
            <div style='background: linear-gradient(135deg, #F5FFF8 0%, #FFF 100%); padding: 20px; border-radius: 12px; border-left: 5px solid #27AE60; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #27AE60; margin-bottom: 8px;'>📌 3. 来源可追溯</div>
                <div style='font-size: 14px;'>所有数据必须标注来自哪份导入的文献，100%可追溯</div>
            </div>
            <div style='background: linear-gradient(135deg, #FFF9F0 0%, #FFF 100%); padding: 20px; border-radius: 12px; border-left: 5px solid #E67E22; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #E67E22; margin-bottom: 8px;'>📌 4. 四维度统计排序</div>
                <div style='font-size: 14px;'>对导入文献进行时效性、交叉印证、项目相关性、内容价值四维度统计并排序</div>
            </div>
            <div style='background: linear-gradient(135deg, #F5F0FF 0%, #FFF 100%); padding: 20px; border-radius: 12px; border-left: 5px solid #9B59B6; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #9B59B6; margin-bottom: 8px;'>📌 5. 隐私保护</div>
                <div style='font-size: 14px;'>所有用户数据、API Key仅本地存储，绝不上传任何服务器</div>
            </div>
        </div>
        """)
        rules_layout.addWidget(rules_text)
        
        layout.addWidget(rules_group)

        info_group = QGroupBox("使用说明")
        info_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(30, 30, 30, 30)
        
        info_text = QTextEdit()
        info_text.setReadOnly(True)
        info_text.setMinimumHeight(200)
        info_text.setStyleSheet("""
            QTextEdit {
                border: none;
                background: transparent;
                font-size: 14px;
                color: #555555;
            }
        """)
        info_text.setHtml("""
        <div style='line-height: 2.2; color: #2C3E50;'>
            <div style='background: linear-gradient(135deg, #F0F7FF 0%, #FFF 100%); padding: 18px; border-radius: 12px; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #3498DB; margin-bottom: 8px;'>📊 6类统计输出</div>
                <div style='font-size: 14px; color: #555;'>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>领域现状</span>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>用户分析</span>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>政策支撑</span>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>痛点缺口</span>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>竞争力对比</span>
                    <span style='display:inline-block;padding:4px 12px;background:#E8F4FD;border-radius:15px;margin:3px 6px 3px 0;'>创新点支撑</span>
                </div>
            </div>
            
            <div style='background: linear-gradient(135deg, #F5FFF8 0%, #FFF 100%); padding: 18px; border-radius: 12px; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #27AE60; margin-bottom: 8px;'>✅ 四维度统计排序</div>
                <div style='font-size: 14px; color: #555;'>
                    <div style='margin:6px 0;'><span style='color:#4A90E2;font-weight:bold;'>时效性(25分)</span> + 
                    <span style='color:#9B59B6;font-weight:bold;'>交叉印证度(15分)</span> + 
                    <span style='color:#E67E22;font-weight:bold;'>项目相关性(50分)</span> + 
                    <span style='color:#27AE60;font-weight:bold;'>内容价值(10分)</span>，按项目相关性优先排序</div>
                </div>
            </div>
            
            <div style='background: linear-gradient(135deg, #FFF9F0 0%, #FFF 100%); padding: 18px; border-radius: 12px; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #E67E22; margin-bottom: 8px;'>🎯 项目申报书分析</div>
                <div style='font-size: 14px; color: #555;'>深度解析您导入的项目申报书，提取主题、研究问题、创新点、关键技术等，用于指导文献筛选和统计</div>
            </div>
            
            <div style='background: linear-gradient(135deg, #F5F0FF 0%, #FFF 100%); padding: 18px; border-radius: 12px; margin: 12px 0;'>
                <div style='font-size: 15px; font-weight: bold; color: #9B59B6; margin-bottom: 8px;'>📚 导入内容优先级</div>
                <div style='font-size: 14px; color: #555;'>参考文献 > 网页内容 > 项目申报书(仅用于理解项目，不作为输出来源)</div>
            </div>
        </div>
        """)
        info_layout.addWidget(info_text)
        
        layout.addWidget(info_group)
        layout.addStretch()
        
        config_scroll.setWidget(config_tab)
        tab_widget.addTab(config_scroll, "API 配置 & 规则")

    def setup_main_tab(self, tab_widget):
        main_scroll = QScrollArea()
        main_scroll.setWidgetResizable(True)
        main_scroll.setFrameShape(QScrollArea.NoFrame)
        
        main_tab = QWidget()
        layout = QVBoxLayout(main_tab)
        layout.setSpacing(20)
        layout.setContentsMargins(40, 40, 40, 40)

        title_group = QGroupBox("项目标题")
        title_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        title_layout = QVBoxLayout(title_group)
        title_layout.setContentsMargins(30, 30, 30, 30)
        
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("请输入大创项目标题")
        self.title_edit.setStyleSheet("""
            QLineEdit {
                padding: 15px 18px;
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                font-size: 14px;
                min-height: 50px;
            }
        """)
        title_layout.addWidget(self.title_edit)
        
        layout.addWidget(title_group)

        file_group = QGroupBox("文件导入（可选，支持PDF/Word/TXT）")
        file_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        file_layout = QGridLayout(file_group)
        file_layout.setSpacing(15)
        file_layout.setContentsMargins(30, 30, 30, 30)

        label1 = QLabel("项目申报书：")
        label1.setMinimumWidth(120)
        label1.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        label1.setStyleSheet("color: #9B59B6; padding: 8px 0;")
        self.project_file_label = QLabel("未选择文件")
        self.project_file_label.setStyleSheet("""
            QLabel {
                padding: 10px 15px;
                background: #F8F9FA;
                border-radius: 8px;
                color: #666666;
            }
        """)
        
        project_btn_layout = QVBoxLayout()
        project_btn = ModernButton("导入申报书", "#9B59B6", "#8E44AD")
        project_btn.clicked.connect(self.import_project_file)
        project_btn.setMaximumWidth(120)
        project_btn_layout.addWidget(project_btn)
        
        file_layout.addWidget(label1, 0, 0)
        file_layout.addWidget(self.project_file_label, 0, 1)
        file_layout.addLayout(project_btn_layout, 0, 2)

        label2 = QLabel("参考文献：")
        label2.setMinimumWidth(120)
        label2.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        label2.setStyleSheet("color: #E67E22; padding: 8px 0;")
        self.ref_file_label = QLabel("未选择文件")
        self.ref_file_label.setStyleSheet("""
            QLabel {
                padding: 10px 15px;
                background: #F8F9FA;
                border-radius: 8px;
                color: #666666;
            }
        """)
        
        ref_btn_layout = QVBoxLayout()
        ref_btn = ModernButton("导入参考文献", "#E67E22", "#D35400")
        ref_btn.clicked.connect(self.import_reference_file)
        ref_btn.setMaximumWidth(120)
        ref_btn_layout.addWidget(ref_btn)
        
        file_layout.addWidget(label2, 1, 0)
        file_layout.addWidget(self.ref_file_label, 1, 1)
        file_layout.addLayout(ref_btn_layout, 1, 2)
        
        file_layout.setColumnStretch(1, 1)
        layout.addWidget(file_group)

        self.project_info_group = QGroupBox("📊 文档分析结果 & 全链路分析")
        self.project_info_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        self.project_info_group.setVisible(False)
        info_layout = QVBoxLayout(self.project_info_group)
        info_layout.setContentsMargins(30, 30, 30, 30)
        
        self.project_info_text = QTextEdit()
        self.project_info_text.setReadOnly(True)
        self.project_info_text.setMaximumHeight(280)
        self.project_info_text.setStyleSheet("""
            QTextEdit {
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                padding: 15px;
                font-size: 13px;
                line-height: 1.8;
            }
        """)
        info_layout.addWidget(self.project_info_text)
        
        layout.addWidget(self.project_info_group)

        progress_group = QGroupBox("执行进度")
        progress_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(30, 30, 30, 30)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #E8ECF1;
                border-radius: 10px;
                text-align: center;
                height: 35px;
                background: #F8F9FA;
            }
            QProgressBar::chunk {
                background-color: #4A90E2;
                border-radius: 8px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("准备就绪")
        self.status_label.setFont(QFont("Microsoft YaHei UI", 12))
        self.status_label.setStyleSheet("color: #7F8C8D;")
        self.status_label.setAlignment(Qt.AlignCenter)
        progress_layout.addWidget(self.status_label)
        
        layout.addWidget(progress_group)

        generate_btn_layout = QHBoxLayout()
        generate_btn = ModernButton("开始生成数据", "#4A90E2", "#357ABD")
        generate_btn.setMinimumHeight(55)
        generate_btn.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        generate_btn.clicked.connect(self.generate_data)
        generate_btn.setMaximumWidth(350)
        generate_btn_layout.addStretch()
        generate_btn_layout.addWidget(generate_btn)
        generate_btn_layout.addStretch()
        layout.addLayout(generate_btn_layout)

        layout.addStretch()
        
        main_scroll.setWidget(main_tab)
        tab_widget.addTab(main_scroll, "生成数据")

    def setup_result_tab(self, tab_widget):
        result_scroll = QScrollArea()
        result_scroll.setWidgetResizable(True)
        result_scroll.setFrameShape(QScrollArea.NoFrame)
        
        result_tab = QWidget()
        layout = QVBoxLayout(result_tab)
        layout.setSpacing(20)
        layout.setContentsMargins(40, 40, 40, 40)

        result_group = QGroupBox("📋 四维度统计分析结果数据（带来源标注）")
        result_group.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        result_layout = QVBoxLayout(result_group)
        result_layout.setContentsMargins(30, 30, 30, 30)
        
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(480)
        self.result_text.setStyleSheet("""
            QTextEdit {
                border: 2px solid #E8ECF1;
                border-radius: 8px;
                background: white;
                padding: 20px;
                font-size: 14px;
                line-height: 1.8;
            }
        """)
        result_layout.addWidget(self.result_text)
        
        layout.addWidget(result_group)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)
        
        export_excel_btn = ModernButton("导出 Excel", "#27AE60", "#229954")
        export_excel_btn.clicked.connect(self.export_excel)
        export_excel_btn.setMaximumWidth(200)
        export_excel_btn.setMinimumHeight(50)
        
        export_word_btn = ModernButton("导出 Word", "#E74C3C", "#C0392B")
        export_word_btn.clicked.connect(self.export_word)
        export_word_btn.setMaximumWidth(200)
        export_word_btn.setMinimumHeight(50)
        
        btn_layout.addStretch()
        btn_layout.addWidget(export_excel_btn)
        btn_layout.addWidget(export_word_btn)
        btn_layout.addStretch()
        
        layout.addLayout(btn_layout)
        
        result_scroll.setWidget(result_tab)
        tab_widget.addTab(result_scroll, "结果展示 & 导出")

    def setup_log_tab(self, tab_widget):
        """日志查看标签页：在前端实时查看程序运行日志"""
        log_scroll = QScrollArea()
        log_scroll.setWidgetResizable(True)
        log_scroll.setFrameShape(QScrollArea.NoFrame)

        log_tab = QWidget()
        layout = QVBoxLayout(log_tab)
        layout.setSpacing(15)
        layout.setContentsMargins(30, 30, 30, 30)

        # 标题
        title = QLabel("📋 程序运行日志")
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Bold))
        title.setStyleSheet("color: #2C3E50; padding: 8px 0;")
        layout.addWidget(title)

        # 说明文字
        hint = QLabel("此处显示程序运行的详细日志，出错时可在本页查看报错信息，无需打开日志文件。")
        hint.setFont(QFont("Microsoft YaHei UI", 11))
        hint.setStyleSheet("color: #7F8C8D; padding: 0 0 8px 0;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        refresh_btn = QPushButton("🔄 刷新日志")
        refresh_btn.setFixedSize(140, 40)
        refresh_btn.setStyleSheet("""
            QPushButton {
                background: #4A90E2;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #357ABD; }
        """)
        refresh_btn.clicked.connect(self._refresh_log_view)

        clear_btn = QPushButton("🗑️ 清空显示")
        clear_btn.setFixedSize(140, 40)
        clear_btn.setStyleSheet("""
            QPushButton {
                background: #E74C3C;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #C0392B; }
        """)
        clear_btn.clicked.connect(self._clear_log_view)

        open_log_btn = QPushButton("📂 打开日志文件")
        open_log_btn.setFixedSize(160, 40)
        open_log_btn.setStyleSheet("""
            QPushButton {
                background: #27AE60;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #1E8449; }
        """)
        open_log_btn.clicked.connect(self._open_log_file)

        btn_layout.addWidget(refresh_btn)
        btn_layout.addWidget(clear_btn)
        btn_layout.addWidget(open_log_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 日志显示区域
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setFont(QFont("Consolas", 11))
        self.log_text_edit.setStyleSheet("""
            QTextEdit {
                background: #1E1E1E;
                color: #D4D4D4;
                border: 1px solid #3C3C3C;
                border-radius: 8px;
                padding: 12px;
            }
        """)
        self.log_text_edit.setMinimumHeight(400)
        layout.addWidget(self.log_text_edit)

        # 状态标签
        self.log_status_label = QLabel("正在加载日志...")
        self.log_status_label.setFont(QFont("Microsoft YaHei UI", 10))
        self.log_status_label.setStyleSheet("color: #95A5A6; padding: 4px 0;")
        layout.addWidget(self.log_status_label)

        log_scroll.setWidget(log_tab)
        tab_widget.addTab(log_scroll, "📋 日志查看")

        # 初始化日志内容 & 启动自动刷新
        self._refresh_log_view()
        self._start_log_timer()

    def _get_current_log_path(self):
        """获取当日日志文件路径"""
        try:
            import logger as logger_module
            return logger_module.get_logger().get_log_file_path()
        except:
            return None

    def _refresh_log_view(self):
        """读取日志文件并显示在文本框中"""
        log_path = self._get_current_log_path()
        if not log_path or not os.path.exists(log_path):
            # 日志文件尚不存在，尝试手动触发一次日志记录
            try:
                from logger import get_logger
                get_logger().info("日志查看页已加载")
                log_path = self._get_current_log_path()
            except:
                pass

        if not log_path or not os.path.exists(log_path):
            self.log_text_edit.setHtml(
                '<span style="color:#F39C12;">⚠️ 暂无日志文件，请先运行程序操作以生成日志。</span>'
            )
            self.log_status_label.setText("日志文件尚未生成")
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)
            # 最多显示最后 500 行，避免卡顿
            if total_lines > 500:
                lines = ["...（仅显示最后 500 行）...\n"] + all_lines[-500:]
                self.log_status_label.setText(f"日志文件：{os.path.basename(log_path)}  （显示最后 500 行，共 {total_lines} 行）")
            else:
                lines = all_lines
                self.log_status_label.setText(f"日志文件：{os.path.basename(log_path)}  （共 {total_lines} 行）")

            # 按行解析，给不同日志级别上色
            html_lines = []
            for line in lines:
                line = line.rstrip()
                if not line:
                    html_lines.append("")
                    continue

                escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                if " - CRITICAL - " in line:
                    html_lines.append(f'<span style="color:#FF0000;font-weight:bold;">{escaped}</span>')
                elif " - ERROR - " in line:
                    html_lines.append(f'<span style="color:#E74C3C;">{escaped}</span>')
                elif " - WARNING - " in line:
                    html_lines.append(f'<span style="color:#F39C12;">{escaped}</span>')
                elif " - DEBUG - " in line:
                    html_lines.append(f'<span style="color:#7F8C8D;">{escaped}</span>')
                elif " - INFO - " in line:
                    html_lines.append(f'<span style="color:#3498DB;">{escaped}</span>')
                else:
                    html_lines.append(f'<span style="color:#D4D4D4;">{escaped}</span>')

            self.log_text_edit.setHtml("<pre>" + "\n".join(html_lines) + "</pre>")
            # 滚动到底部
            self.log_text_edit.verticalScrollBar().setValue(
                self.log_text_edit.verticalScrollBar().maximum()
            )
        except Exception as e:
            self.log_text_edit.setHtml(
                f'<span style="color:#E74C3C;">读取日志文件失败：{str(e)}</span>'
            )
            self.log_status_label.setText(f"读取失败：{str(e)}")

    def _clear_log_view(self):
        """清空日志显示区域（不影响日志文件）"""
        self.log_text_edit.clear()
        self.log_status_label.setText("已清空显示（日志文件不受影响）")

    def _open_log_file(self):
        """用系统默认程序打开日志文件"""
        log_path = self._get_current_log_path()
        if not log_path or not os.path.exists(log_path):
            QMessageBox.warning(self, "提示", "日志文件尚未生成，请先运行程序。")
            return
        try:
            os.startfile(log_path)
        except Exception as e:
            QMessageBox.warning(self, "提示", f"无法打开日志文件：{str(e)}")

    def _start_log_timer(self):
        """启动定时器，每 3 秒自动刷新日志"""
        from PyQt5.QtCore import QTimer
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self._refresh_log_view)
        self.log_timer.start(3000)

    def closeEvent(self, event):
        """关闭窗口时停止定时器"""
        if hasattr(self, 'log_timer'):
            self.log_timer.stop()
        super().closeEvent(event)

    def load_config(self):
        config = self.config_manager.load_config()
        if "model_type" in config:
            model_type = config["model_type"]
            if model_type == "openrouter":
                self.model_combo.setCurrentIndex(0)
            elif model_type == "doubao":
                self.model_combo.setCurrentIndex(1)
            elif model_type == "tongyi":
                self.model_combo.setCurrentIndex(2)
        self.api_key_edit.setText(self.config_manager.get_api_key(self._get_model_key()))
        # 加载百度OCR配置
        baidu_api_key, baidu_secret_key = self.config_manager.get_baidu_keys()
        self.baidu_api_key_edit.setText(baidu_api_key)
        self.baidu_secret_key_edit.setText(baidu_secret_key)

    def _get_model_key(self):
        index = self.model_combo.currentIndex()
        if index == 0:
            return "openrouter"
        elif index == 1:
            return "doubao"
        else:
            return "tongyi"

    def save_config(self):
        model_type = self._get_model_key()
        api_key = self.api_key_edit.text()
        self.config_manager.set_api_key(model_type, api_key)
        config = self.config_manager.load_config()
        config["model_type"] = model_type
        self.config_manager.save_config(config)
        QMessageBox.information(self, "成功", "配置已加密保存！")
    
    def save_ocr_config(self):
        baidu_api_key = self.baidu_api_key_edit.text()
        baidu_secret_key = self.baidu_secret_key_edit.text()
        self.config_manager.set_baidu_keys(baidu_api_key, baidu_secret_key)
        QMessageBox.information(self, "成功", "百度OCR配置已加密保存！")

    def import_project_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择项目申报书", "", "支持的文件 (*.pdf *.docx *.doc *.txt)")
        if file_path:
            self.project_file_label.setText(file_path.split("/")[-1])
            self._parse_file_with_thread(file_path, 'project')

    def import_reference_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择参考文献", "", "支持的文件 (*.pdf *.docx *.doc *.txt)")
        if file_path:
            self.ref_file_label.setText(file_path.split("/")[-1])
            self._parse_file_with_thread(file_path, 'reference')
    
    def _parse_file_with_thread(self, file_path, file_type):
        # 创建并启动文件解析线程
        self.parse_thread = FileParseThread(file_path, file_type, self.config_manager)
        self.parse_thread.progress.connect(self._on_parse_progress)
        self.parse_thread.finished.connect(self._on_parse_finished)
        self.parse_thread.error.connect(self._on_parse_error)
        self.parse_thread.start()
    
    def _on_parse_progress(self, value, message):
        self.progress_bar.setValue(value)
        self.status_label.setText(message)
    
    def _on_parse_finished(self, text, file_type):
        try:
            # 判断文件类型（从label中获取文件名）
            file_label = self.project_file_label.text() if file_type == 'project' else self.ref_file_label.text()
            is_pdf = file_label.lower().endswith('.pdf')
            
            if file_type == 'project':
                self.project_text = text
                
                scraper = WebScraper()
                self.project_info = scraper.extract_project_info(self.project_text)
                
                self.display_project_info()
                
                # 检查是否配置了大模型API（项目申报书也支持数据提取）
                model_type = self._get_model_key()
                api_key = self.config_manager.get_api_key(model_type)
                
                if api_key:
                    # 启动项目申报书数据提取线程
                    reply = QMessageBox.question(
                        self, 
                        "申报书数据提取", 
                        f"是否使用大模型从导入的项目申报书中提取结构化量化数据？\n\n将提取：立项背景、现状问题、创新点、技术路线、预期成果、用户痛点等数据",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes
                    )
                    
                    if reply == QMessageBox.Yes:
                        file_name = self.project_file_label.text()
                        self._extract_paper_data_with_thread(self.project_text, file_type, is_pdf, file_name)
                        return
                else:
                    if is_pdf:
                        QMessageBox.information(self, "PDF解析完成", f"🎉 PDF解析完成！\n\n已识别文字：{len(self.project_text)} 字\n\n项目申报书已成功导入。\n\n如需使用大模型提取结构化数据，请先配置API。")
                    else:
                        QMessageBox.information(self, "成功", f"项目申报书导入成功！\n文档字数：{len(self.project_text)}")
                    return
                
                if is_pdf:
                    QMessageBox.information(self, "PDF解析完成", f"🎉 PDF解析完成！\n\n已识别文字：{len(self.project_text)} 字\n\n项目申报书已成功导入。")
                else:
                    QMessageBox.information(self, "成功", f"项目申报书导入成功！\n文档字数：{len(self.project_text)}")
            else:
                self.reference_text = text
                
                # 先完成基本的文档分析
                scraper = WebScraper()
                ref_info = scraper.analyze_document(self.reference_text, 'reference')
                
                self.display_project_info()
                
                # 检查是否配置了大模型API
                model_type = self._get_model_key()
                api_key = self.config_manager.get_api_key(model_type)
                
                if api_key:
                    # 启动论文数据提取线程
                    reply = QMessageBox.question(
                        self, 
                        "数据提取", 
                        f"是否使用大模型从导入的文献中提取结构化量化数据？\n\n将提取：领域现状、用户画像、痛点问题、行业缺口、政策支撑、创新点支撑等6类量化数据",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes
                    )
                    
                    if reply == QMessageBox.Yes:
                        file_name = self.ref_file_label.text()
                        self._extract_paper_data_with_thread(self.reference_text, file_type, is_pdf, file_name)
                        return
                else:
                    QMessageBox.information(
                        self, 
                        "提示", 
                        f"文献解析完成！\n\n如需使用大模型提取结构化量化数据，请先在「API配置&规则」页面配置大模型API。\n\n文档字数：{len(self.reference_text)}"
                    )
                
                if is_pdf:
                    QMessageBox.information(self, "PDF解析完成", f"🎉 PDF解析完成！\n\n已识别文字：{len(self.reference_text)} 字\n\n参考文献已成功导入，\n将用于项目-文献相关性分析。")
                else:
                    QMessageBox.information(self, "成功", f"参考文献导入成功！\n文档字数：{len(self.reference_text)}")
        
        except Exception as e:
            QMessageBox.warning(self, "错误", f"文件处理失败: {str(e)}")
        
        finally:
            self.progress_bar.setValue(0)
            self.status_label.setText("准备就绪")
    
    def _extract_paper_data_with_thread(self, paper_text, file_type, is_pdf, file_name=""):
        """启动论文/申报书数据提取线程"""
        model_type = self._get_model_key()
        api_key = self.config_manager.get_api_key(model_type)
        
        self.paper_extract_thread = PaperDataExtractThread(paper_text, file_type, model_type, api_key, file_name)
        self.paper_extract_thread.progress.connect(self._on_paper_extract_progress)
        self.paper_extract_thread.finished.connect(self._on_paper_extract_finished)
        self.paper_extract_thread.error.connect(self._on_paper_extract_error)
        self.paper_extract_thread.start()
        
        # 保存is_pdf标记，用于后续提示
        self.current_is_pdf = is_pdf
    
    def _on_paper_extract_progress(self, value, message):
        self.progress_bar.setValue(value)
        self.status_label.setText(message)
    
    def _on_paper_extract_finished(self, extracted_data, file_type):
        try:
            # 明确分离立项书数据和文献数据
            if file_type == 'project':
                # 立项书数据单独保存
                if not hasattr(self, 'extracted_project_data'):
                    self.extracted_project_data = []
                self.extracted_project_data.extend(extracted_data)
                data_source_name = "项目申报书"
            else:
                # 参考文献数据单独保存
                if not hasattr(self, 'extracted_reference_data'):
                    self.extracted_reference_data = []
                self.extracted_reference_data.extend(extracted_data)
                data_source_name = "参考文献"
            
            # 更新界面显示
            self.display_project_info()
            
            # 显示提取结果
            data_count = len(extracted_data)
            
            if data_count > 0:
                result_msg = f"🎉 {data_source_name}数据提取完成！\n\n成功提取 {data_count} 条量化数据：\n\n"
                
                # 按类别统计
                category_counts = {}
                for item in extracted_data:
                    category = item.get('category', item.get('tag', '其他'))
                    category_counts[category] = category_counts.get(category, 0) + 1
                
                for category, count in category_counts.items():
                    result_msg += f"• {category}: {count} 条\n"
                
                result_msg += f"\n{data_source_name}数据已显示在下方。"
                QMessageBox.information(self, "提取完成", result_msg)
            else:
                QMessageBox.information(self, "提示", f"未从{data_source_name}中提取到量化数据。")
        
        except Exception as e:
            QMessageBox.warning(self, "错误", f"处理提取结果时出错: {str(e)}")
        
        finally:
            self.progress_bar.setValue(0)
            self.status_label.setText("准备就绪")
    
    def _on_paper_extract_error(self, error_msg):
        self.progress_bar.setValue(0)
        self.status_label.setText("准备就绪")
        QMessageBox.warning(self, "数据提取失败", f"论文数据提取失败:\n{error_msg}\n\n文献内容仍可用于项目-文献相关性分析。")
    
    def _on_parse_error(self, error_msg):
        self.progress_bar.setValue(0)
        self.status_label.setText("准备就绪")
        QMessageBox.warning(self, "错误", f"文件解析失败:\n{error_msg}")

    def display_project_info(self):
        self.project_info_group.setVisible(True)
        
        info_html = "<div style='line-height: 1.8;'>"
        
        # 检查是否有立项书数据
        has_project_data = hasattr(self, 'extracted_project_data') and self.extracted_project_data
        has_reference_data = hasattr(self, 'extracted_reference_data') and self.extracted_reference_data
        
        # 辅助函数：显示分类数据
        def display_category_data(items, title, color):
            nonlocal info_html
            info_html += f"<h4 style='color: {color}; margin: 20px 0 15px 0; border-bottom: 2px solid #E8ECF1; padding-bottom: 10px;'>{title}</h4>"
            
            # 按类别分组显示
            category_data = {}
            for item in items:
                category = item.get('category', item.get('tag', '其他'))
                if category not in category_data:
                    category_data[category] = []
                category_data[category].append(item)
            
            # 定义类别顺序
            category_order = [
                '领域现状', '行业现状',
                '用户画像', '目标用户',
                '痛点问题', '用户痛点',
                '创新点支撑', '创新点',
                '政策支撑',
                '行业缺口',
                '其他'
            ]
            
            # 按顺序显示类别
            for category in category_order:
                if category in category_data:
                    items_in_category = category_data[category]
                    color_map = {
                        '领域现状': '#3498DB',
                        '行业现状': '#3498DB',
                        '用户画像': '#E67E22',
                        '目标用户': '#E67E22',
                        '痛点问题': '#E74C3C',
                        '用户痛点': '#E74C3C',
                        '创新点支撑': '#F39C12',
                        '创新点': '#F39C12',
                        '政策支撑': '#16A085',
                        '行业缺口': '#9B59B6',
                        '其他': '#7F8C8D'
                    }
                    
                    info_html += f"<div style='margin-top: 12px;'>"
                    default_color = "#2C3E50"
                    category_color = color_map.get(category, default_color)
                    info_html += f"<p style='margin: 8px 0; font-weight: bold; color: {category_color};'>• {category} ({len(items_in_category)}条):</p>"
                    
                    for item in items_in_category:
                        content = item.get('content', item.get('data', ''))
                        source = item.get('source', '导入文献')
                        info_html += f"<p style='margin: 5px 0; padding-left: 20px; color: #555;'>- {content}【{source}】</p>"
                    
                    info_html += f"</div>"
        
        # 显示立项书数据
        if has_project_data:
            display_category_data(self.extracted_project_data, "📋 项目申报书数据（用于理解项目思路）", "#9B59B6")
        
        # 显示参考文献数据
        if has_reference_data:
            display_category_data(self.extracted_reference_data, "📚 参考文献数据（用于分析支持）", "#E67E22")
        
        # 如果都没有数据，显示提示
        if not has_project_data and not has_reference_data:
            info_html += f"<div style='padding: 20px; background: #F8F9FA; border-radius: 8px; text-align: center;'>"
            info_html += f"<p style='color: #7F8C8D; font-size: 16px;'>💡 导入申报书或文献后选择「提取量化数据」</p>"
            info_html += f"<p style='color: #7F8C8D; font-size: 14px; margin-top: 10px;'>将自动提取结构化数据并分别展示</p>"
            info_html += f"</div>"
        
        info_html += "</div>"
        
        self.project_info_text.setHtml(info_html)

    def generate_data(self):
        model_type = self._get_model_key()
        api_key = self.api_key_edit.text()
        title = self.title_edit.text().strip()

        if not api_key:
            QMessageBox.warning(self, "提示", "请先在API配置页面设置API Key！")
            return

        if not title and not self.project_text:
            QMessageBox.warning(self, "提示", "请输入项目标题或导入项目申报书！")
            return
        
        # 获取分离的立项书数据和参考文献数据
        extracted_project_data = getattr(self, 'extracted_project_data', None)
        extracted_reference_data = getattr(self, 'extracted_reference_data', None)

        self.generate_thread = GenerateThread(
            model_type, api_key, title,
            self.project_text,
            self.reference_text,
            self.project_info,
            extracted_project_data,
            extracted_reference_data
        )
        self.generate_thread.progress.connect(self.update_progress)
        self.generate_thread.finished.connect(self.on_finished)
        self.generate_thread.error.connect(self.on_error)
        self.generate_thread.start()

    def update_progress(self, value, message):
        self.progress_bar.setValue(value)
        self.status_label.setText(message)

    def on_finished(self, data):
        self.current_data = data
        self.display_result(data)
        QMessageBox.information(self, "完成", "数据生成完成！已通过四维度审核！")

    def on_error(self, error_msg):
        QMessageBox.critical(self, "错误", f"生成失败:\n{error_msg}")

    def display_result(self, data):
        result_html = """
        <div style='padding: 10px;'>
            <h2 style='color: #2C3E50; text-align: center; margin-bottom: 20px;'>
                🎯 大创数据全链路分析报告
            </h2>
            <div style='background: #F8F9FA; padding: 15px; border-radius: 10px; margin-bottom: 25px;'>
                <p style='margin: 5px 0; color: #555;'>
                    <b>📊 评分权重调整：</b>时效性(30分) | 交叉印证(25分) | 
                    <span style='color: #E74C3C;'>相关性(25分,降低)</span> | 内容价值(20分)
                </p>
            </div>
        """
        
        for title, items in data.items():
            # 根据标题颜色区分
            title_color = "#4CAF50" if "优先" in title else "#4A90E2" if "参考文献" in title else "#9B59B6"
            result_html += f"<h3 style='color: {title_color}; margin-top: 25px; font-size: 17px; border-bottom: 2px solid #E8ECF1; padding-bottom: 8px;'>{title}</h3>"
            
            if isinstance(items, list):
                result_html += "<ul style='margin-left: 5px; list-style: none; padding: 0;'>"
                
                for item in items:
                    if isinstance(item, dict):
                        data_str = item.get('数据', item.get('data', item.get('content', item.get('title', ''))))
                        source_str = item.get('来源', item.get('source', ''))
                        score = item.get('audit_score', '')
                        weight = item.get('weight', '')
                        source_type = item.get('source_type', '')
                        score_breakdown = item.get('score_breakdown', {})
                        
                        is_user_doc = source_type == 'user_document' or item.get('skip_audit')
                        
                        # 优先级区分样式
                        if "优先" in title:
                            result_html += f"<li style='margin: 15px 0; padding: 18px; background: #FFF8E1; border-radius: 10px; border-left: 5px solid #FF9800;'>"
                        elif is_user_doc:
                            result_html += f"<li style='margin: 15px 0; padding: 18px; background: #E8F5E9; border-radius: 10px; border-left: 5px solid #4CAF50;'>"
                        else:
                            result_html += f"<li style='margin: 15px 0; padding: 18px; background: #F8F9FA; border-radius: 10px; border-left: 5px solid #4A90E2;'>"
                        
                        result_html += f"<div style='font-weight: bold; color: #2C3E50; font-size: 15px;'>{data_str}</div>"
                        
                        if source_str:
                            result_html += f"<div style='color: #555555; margin-top: 10px; font-size: 13px;'><b>来源：</b>{source_str}</div>"
                        
                        meta_parts = []
                        if source_type:
                            type_labels = {
                                'government': '官方/政府',
                                'academic': '学术',
                                'official': '官方',
                                'reputable_media': '权威媒体',
                                'user_document': '用户文档(免审核)',
                                'web_search': '网络搜索',
                                'web_scrape': '网页抓取',
                                'user_upload': '用户上传'
                            }
                            meta_parts.append(type_labels.get(source_type, source_type))
                        
                        if score:
                            meta_parts.append(f"审核分: {score}")
                        
                        if weight:
                            meta_parts.append(f"权重: {weight}")
                        
                        if meta_parts:
                            result_html += f"<div style='color: #7F8C8D; margin-top: 8px; font-size: 12px;'>{' | '.join(meta_parts)}</div>"
                        
                        if score_breakdown and not is_user_doc:
                            detail_parts = []
                            if 'timeliness' in score_breakdown:
                                tl_score = score_breakdown['timeliness']
                                tl_color = "#27AE60" if tl_score >= 24 else "#F39C12" if tl_score >= 15 else "#E74C3C"
                                detail_parts.append(f"<span style='color:{tl_color};'>时效:{tl_score}</span>")
                            if 'cross_validation' in score_breakdown:
                                cv_score = score_breakdown['cross_validation']
                                cv_color = "#27AE60" if cv_score >= 20 else "#F39C12" if cv_score >= 10 else "#E74C3C"
                                detail_parts.append(f"<span style='color:{cv_color};'>交叉印证:{cv_score}</span>")
                            if 'relevance' in score_breakdown:
                                rel_score = score_breakdown['relevance']
                                rel_color = "#27AE60" if rel_score >= 20 else "#F39C12" if rel_score >= 10 else "#E74C3C"
                                detail_parts.append(f"<span style='color:{rel_color};'>相关性:{rel_score}</span>")
                            if 'content_value' in score_breakdown:
                                cv_score = score_breakdown['content_value']
                                cv_color = "#27AE60" if cv_score >= 16 else "#F39C12" if cv_score >= 10 else "#E74C3C"
                                detail_parts.append(f"<span style='color:{cv_color};'>内容价值:{cv_score}</span>")
                            
                            if detail_parts:
                                result_html += f"<div style='background: #E8F4FD; padding: 8px 12px; border-radius: 6px; margin-top: 10px; font-size: 12px; color: #2C3E50;'>四维度评分详情: {' | '.join(detail_parts)}</div>"
                        
                        result_html += "</li>"
                    else:
                        result_html += f"<li style='margin: 10px 0;'>{item}</li>"
                result_html += "</ul>"
            else:
                result_html += f"<p style='color: #555555;'>{items}</p>"
        
        result_html += "</div>"
        self.result_text.setHtml(result_html)

    def export_excel(self):
        if not self.current_data:
            QMessageBox.warning(self, "提示", "没有可导出的数据！")
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "导出 Excel", "大创项目背景数据.xlsx", "Excel文件 (*.xlsx)")
        if file_path:
            try:
                Exporter.export_to_excel(self.current_data, file_path)
                QMessageBox.information(self, "成功", "导出Excel成功！")
            except Exception as e:
                QMessageBox.warning(self, "错误", f"导出失败: {str(e)}")

    def export_word(self):
        if not self.current_data:
            QMessageBox.warning(self, "提示", "没有可导出的数据！")
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "导出 Word", "大创项目背景数据.docx", "Word文件 (*.docx)")
        if file_path:
            try:
                Exporter.export_to_word(self.current_data, file_path)
                QMessageBox.information(self, "成功", "导出Word成功！")
            except Exception as e:
                QMessageBox.warning(self, "错误", f"导出失败: {str(e)}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    # 设置中文字体，确保中文正确显示
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    
    # 为不同组件设置字体
    title_font = QFont("Microsoft YaHei UI", 12, QFont.Bold)
    app.setFont(title_font, "QLabel")
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

