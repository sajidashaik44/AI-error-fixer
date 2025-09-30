from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiohttp
import asyncio
import re
import time
import hashlib
from typing import Optional, Dict, Any, List
import logging
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enhanced Data Models
class ErrorRequest(BaseModel):
    error_message: str
    code_snippet: str
    file_path: str
    line_number: int

class BatchErrorItem(BaseModel):
    error_message: str
    code_snippet: str
    line_number: int
    error_id: str
    context: Optional[Dict[str, Any]] = None

class BatchErrorRequest(BaseModel):
    errors: List[BatchErrorItem]
    file_path: str

class ConsolidatedFix(BaseModel):
    primary_fix: str
    primary_explanation: str
    primary_confidence: float
    alternative_fix: str
    alternative_explanation: str
    alternative_confidence: float
    errors_fixed: List[str]  # List of error descriptions
    total_errors: int

class ConsolidatedFixResponse(BaseModel):
    consolidated_fix: ConsolidatedFix
    processing_time: float
    success: bool

# Cache System
class FixCache:
    def __init__(self, max_size: int = 1000):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.access_times: Dict[str, float] = {}
        self.max_size = max_size
        self.hit_count = 0
        self.total_requests = 0

    def _make_key(self, errors_signature: str) -> str:
        return hashlib.md5(errors_signature.encode()).hexdigest()

    def get(self, errors_signature: str) -> Optional[Dict[str, Any]]:
        self.total_requests += 1
        key = self._make_key(errors_signature)
        if key in self.cache:
            self.hit_count += 1
            self.access_times[key] = time.time()
            return self.cache[key]
        return None

    def set(self, errors_signature: str, fix: Dict[str, Any]) -> None:
        key = self._make_key(errors_signature)
        
        if len(self.cache) >= self.max_size:
            oldest_key = min(self.access_times, key=self.access_times.get)
            del self.cache[oldest_key]
            del self.access_times[oldest_key]

        self.cache[key] = fix
        self.access_times[key] = time.time()

    def stats(self) -> Dict[str, Any]:
        hit_rate = self.hit_count / max(self.total_requests, 1)
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hit_rate": hit_rate,
            "hit_count": self.hit_count,
            "total_requests": self.total_requests
        }

# Global cache instance
fix_cache = FixCache(max_size=1000)

# Enhanced Error Parser
class ErrorParser:
    @staticmethod
    def parse_python_error(error_message: str) -> Dict[str, str]:
        """Enhanced Python error parsing"""
        lines = error_message.strip().split('\n')
        error_line = lines[-1] if lines else ""

        # Enhanced error type detection
        error_patterns = [
            (r'"(\[|\(|\{)" was not closed', 'SyntaxError'),
            (r'"(\]|\)|\})" was never opened', 'SyntaxError'),
            (r'(\w*Error):', r'\1'),
            (r'(\w*Exception):', r'\1'),
            (r'(\w*Warning):', r'\1'),
        ]

        error_type = "SyntaxError"
        error_detail = error_line

        for pattern, replacement in error_patterns:
            match = re.search(pattern, error_line)
            if match:
                if replacement in ['SyntaxError']:
                    error_type = replacement
                    error_detail = error_line
                else:
                    error_type = match.group(1)
                    error_detail = re.sub(pattern, '', error_line).strip()
                break

        return {
            "error_type": error_type,
            "error_detail": error_detail,
            "full_traceback": error_message
        }

# Clean Code Extractor
class CleanCodeExtractor:
    @staticmethod
    def extract_clean_code(code_snippet: str) -> str:
        """Extract clean code without line numbers and markers"""
        lines = code_snippet.split('\n')
        clean_lines = []
        
        for line in lines:
            # Skip comment lines that are just context
            if line.strip().startswith('# File imports:') or line.strip().startswith('# Function context:'):
                continue
            
            # Remove line numbers and markers
            clean_line = line
            
            # Remove >>> markers
            if '>>>' in clean_line:
                clean_line = clean_line.replace('>>>', '').strip()
                # Find the indentation from the original line
                original_indent = len(line) - len(line.lstrip())
                clean_line = ' ' * (original_indent - 4) + clean_line  # Adjust for >>> removal
            
            # Remove line number patterns like "    23: "
            clean_line = re.sub(r'^\s*\d+:\s*', '', clean_line)
            
            # Skip empty lines that were just line numbers
            if clean_line.strip() or not line.strip():
                clean_lines.append(clean_line)
        
        return '\n'.join(clean_lines).strip()

# Enhanced Consolidated Fix Generator
class ConsolidatedFixGenerator:
    @staticmethod
    async def check_ollama_status() -> bool:
        """Check if Ollama is running"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://localhost:11434/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return response.status == 200
        except:
            return False

    @staticmethod
    async def generate_consolidated_fix(batch_request: BatchErrorRequest) -> ConsolidatedFix:
        """Generate one consolidated fix for all errors"""
        
        # Create signature for caching
        errors_signature = '|'.join([f"{e.error_message}:{e.line_number}" for e in batch_request.errors])
        
        # Check cache
        cached_fix = fix_cache.get(errors_signature)
        if cached_fix:
            return ConsolidatedFix(**cached_fix)

        # Extract clean code from the first error (they should all have the same code)
        clean_code = CleanCodeExtractor.extract_clean_code(batch_request.errors[0].code_snippet)
        
        # Parse all errors
        parsed_errors = []
        error_descriptions = []
        
        for error_item in batch_request.errors:
            parsed_error = ErrorParser.parse_python_error(error_item.error_message)
            parsed_errors.append(parsed_error)
            error_descriptions.append(f"Line {error_item.line_number}: {parsed_error['error_detail']}")

        # Generate consolidated fix
        primary_fix, alternative_fix, explanations, confidences = await ConsolidatedFixGenerator._generate_fixes(
            clean_code, parsed_errors, batch_request.file_path
        )

        consolidated_fix = ConsolidatedFix(
            primary_fix=primary_fix,
            primary_explanation=explanations['primary'],
            primary_confidence=confidences['primary'],
            alternative_fix=alternative_fix,
            alternative_explanation=explanations['alternative'],
            alternative_confidence=confidences['alternative'],
            errors_fixed=error_descriptions,
            total_errors=len(batch_request.errors)
        )

        # Cache the result
        fix_cache.set(errors_signature, consolidated_fix.dict())
        
        return consolidated_fix

    @staticmethod
    async def _generate_fixes(clean_code: str, parsed_errors: List[Dict], file_path: str) -> tuple:
        """Generate primary and alternative fixes"""
        
        # Try AI-based fix first
        ollama_available = await ConsolidatedFixGenerator.check_ollama_status()
        
        if ollama_available:
            try:
                return await ConsolidatedFixGenerator._generate_ai_fixes(clean_code, parsed_errors, file_path)
            except Exception as e:
                logger.error(f"AI fix generation failed: {e}")
        
        # Fallback to rule-based fixes
        return ConsolidatedFixGenerator._generate_rule_based_fixes(clean_code, parsed_errors)

    @staticmethod
    async def _generate_ai_fixes(clean_code: str, parsed_errors: List[Dict], file_path: str) -> tuple:
        """Generate AI-based fixes"""
        
        # Create consolidated error description
        error_summary = []
        for i, error in enumerate(parsed_errors, 1):
            error_summary.append(f"Error {i}: {error['error_type']} - {error['error_detail']}")
        
        prompt = f"""You are an expert Python debugger. Fix ALL the following errors in this code.

ERRORS TO FIX:
{chr(10).join(error_summary)}

CURRENT CODE:
```python
{clean_code}
```

Provide EXACTLY this format with clean, executable Python code:

PRIMARY_FIX:
```python
[Complete fixed code - ready to copy-paste and run]
```

PRIMARY_EXPLANATION:
[Brief explanation of all fixes applied]

PRIMARY_CONFIDENCE: [0.0 to 1.0]

ALTERNATIVE_FIX:
```python
[Alternative approach to fix the same issues]
```

ALTERNATIVE_EXPLANATION:
[Brief explanation of alternative approach]

ALTERNATIVE_CONFIDENCE: [0.0 to 1.0]

Requirements:
- Provide complete, clean, executable Python code
- Fix ALL syntax errors
- No line numbers or comments in the code
- Ready to copy-paste and run
"""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://localhost:11434/api/generate",
                    json={
                        "model": "codellama:latest",
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.1,
                            "top_p": 0.8,
                            "num_predict": 500,
                            "num_thread": 6,
                            "repeat_penalty": 1.2
                        }
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    
                    if response.status == 200:
                        result = await response.json()
                        ai_response = result.get('response', '')
                        
                        return ConsolidatedFixGenerator._parse_ai_response(ai_response, clean_code)
        
        except Exception as e:
            logger.error(f"AI request failed: {e}")
        
        # Fallback to rule-based
        return ConsolidatedFixGenerator._generate_rule_based_fixes(clean_code, parsed_errors)

    @staticmethod
    def _parse_ai_response(ai_response: str, original_code: str) -> tuple:
        """Parse AI response to extract fixes"""
        
        # Default values
        primary_fix = original_code
        alternative_fix = original_code
        primary_explanation = "AI provided fixes"
        alternative_explanation = "Alternative AI fixes"
        primary_confidence = 0.8
        alternative_confidence = 0.7

        try:
            # Extract PRIMARY_FIX
            primary_match = re.search(r'PRIMARY_FIX:\s*```python\n(.*?)```', ai_response, re.DOTALL)
            if primary_match:
                primary_fix = primary_match.group(1).strip()

            # Extract PRIMARY_EXPLANATION
            primary_exp_match = re.search(r'PRIMARY_EXPLANATION:\s*(.*?)(?=PRIMARY_CONFIDENCE:|ALTERNATIVE_FIX:|$)', ai_response, re.DOTALL)
            if primary_exp_match:
                primary_explanation = primary_exp_match.group(1).strip()

            # Extract PRIMARY_CONFIDENCE
            primary_conf_match = re.search(r'PRIMARY_CONFIDENCE:\s*([0-9.]+)', ai_response)
            if primary_conf_match:
                primary_confidence = min(1.0, max(0.0, float(primary_conf_match.group(1))))

            # Extract ALTERNATIVE_FIX
            alt_match = re.search(r'ALTERNATIVE_FIX:\s*```python\n(.*?)```', ai_response, re.DOTALL)
            if alt_match:
                alternative_fix = alt_match.group(1).strip()

            # Extract ALTERNATIVE_EXPLANATION
            alt_exp_match = re.search(r'ALTERNATIVE_EXPLANATION:\s*(.*?)(?=ALTERNATIVE_CONFIDENCE:|$)', ai_response, re.DOTALL)
            if alt_exp_match:
                alternative_explanation = alt_exp_match.group(1).strip()

            # Extract ALTERNATIVE_CONFIDENCE
            alt_conf_match = re.search(r'ALTERNATIVE_CONFIDENCE:\s*([0-9.]+)', ai_response)
            if alt_conf_match:
                alternative_confidence = min(1.0, max(0.0, float(alt_conf_match.group(1))))

        except Exception as e:
            logger.error(f"Error parsing AI response: {e}")

        explanations = {
            'primary': primary_explanation,
            'alternative': alternative_explanation
        }
        
        confidences = {
            'primary': primary_confidence,
            'alternative': alternative_confidence
        }

        return primary_fix, alternative_fix, explanations, confidences

    @staticmethod
    def _generate_rule_based_fixes(clean_code: str, parsed_errors: List[Dict]) -> tuple:
        """Generate rule-based fixes for common syntax errors"""
        
        primary_fix = clean_code
        alternative_fix = clean_code
        
        fixed_issues = []
        
        # Apply fixes for each error type
        for error in parsed_errors:
            error_type = error['error_type']
            error_detail = error['error_detail']
            
            if error_type == 'SyntaxError':
                if '"[" was not closed' in error_detail:
                    # Fix missing closing brackets
                    open_brackets = primary_fix.count('[')
                    close_brackets = primary_fix.count(']')
                    if open_brackets > close_brackets:
                        missing = open_brackets - close_brackets
                        primary_fix = primary_fix + ']' * missing
                        alternative_fix = primary_fix.replace('[', '', missing)  # Remove extra opening brackets
                        fixed_issues.append(f"Added {missing} missing closing bracket(s)")
                
                elif '"(" was not closed' in error_detail:
                    # Fix missing closing parentheses
                    open_parens = primary_fix.count('(')
                    close_parens = primary_fix.count(')')
                    if open_parens > close_parens:
                        missing = open_parens - close_parens
                        primary_fix = primary_fix + ')' * missing
                        alternative_fix = primary_fix.replace('(', '', missing)  # Remove extra opening parens
                        fixed_issues.append(f"Added {missing} missing closing parenthesis/parentheses")
                
                elif 'Statements must be separated' in error_detail:
                    # This often means missing punctuation
                    if 'writer.writerow' in primary_fix and not primary_fix.rstrip().endswith(')'):
                        primary_fix = primary_fix.rstrip() + ')'
                        alternative_fix = primary_fix
                        fixed_issues.append("Added missing closing parenthesis for function call")

        explanations = {
            'primary': f"Fixed syntax errors: {', '.join(fixed_issues) if fixed_issues else 'Applied standard fixes'}",
            'alternative': f"Alternative approach: {', '.join(fixed_issues) if fixed_issues else 'Applied alternative fixes'}"
        }
        
        confidences = {
            'primary': 0.9 if fixed_issues else 0.6,
            'alternative': 0.8 if fixed_issues else 0.5
        }

        return primary_fix, alternative_fix, explanations, confidences

# Consolidated Batch Processor
class ConsolidatedBatchProcessor:
    @staticmethod
    async def process_consolidated_batch(batch_request: BatchErrorRequest) -> ConsolidatedFixResponse:
        """Process all errors and return one consolidated fix"""
        start_time = time.time()
        
        logger.info(f"Processing consolidated batch of {len(batch_request.errors)} errors")
        
        try:
            consolidated_fix = await ConsolidatedFixGenerator.generate_consolidated_fix(batch_request)
            processing_time = time.time() - start_time
            
            logger.info(f"Consolidated fix generated successfully in {processing_time:.2f}s")
            
            return ConsolidatedFixResponse(
                consolidated_fix=consolidated_fix,
                processing_time=processing_time,
                success=True
            )
            
        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"Consolidated processing failed: {e}")
            
            # Return fallback response
            clean_code = CleanCodeExtractor.extract_clean_code(batch_request.errors[0].code_snippet)
            
            fallback_fix = ConsolidatedFix(
                primary_fix=clean_code,
                primary_explanation=f"Processing failed: {str(e)}",
                primary_confidence=0.1,
                alternative_fix=f"# TODO: Fix errors manually\n{clean_code}",
                alternative_explanation="Manual review required",
                alternative_confidence=0.1,
                errors_fixed=[f"Error processing: {str(e)}"],
                total_errors=len(batch_request.errors)
            )
            
            return ConsolidatedFixResponse(
                consolidated_fix=fallback_fix,
                processing_time=processing_time,
                success=False
            )

# Application setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Consolidated AI Error Fixer API...")
    yield
    logger.info("👋 Shutting down Consolidated AI Error Fixer API...")

app = FastAPI(
    title="Consolidated AI Error Fixer API",
    description="AI-powered error fixing with clean, consolidated output",
    version="5.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Endpoints
@app.get("/health")
async def health_check():
    try:
        ollama_available = await ConsolidatedFixGenerator.check_ollama_status()
        return {
            "status": "healthy",
            "ollama_available": ollama_available,
            "cache_stats": fix_cache.stats(),
            "features": [
                "Consolidated error processing",
                "Clean code output",
                "No line numbers or markers",
                "Ready-to-paste fixes"
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")

@app.post("/fix-errors-consolidated", response_model=ConsolidatedFixResponse)
async def fix_errors_consolidated(request: BatchErrorRequest):
    """Main endpoint for consolidated error fixing"""
    try:
        if not request.errors:
            raise HTTPException(status_code=400, detail="No errors provided")
        
        if len(request.errors) > 50:
            raise HTTPException(status_code=400, detail="Too many errors. Maximum 50 per request.")
        
        result = await ConsolidatedBatchProcessor.process_consolidated_batch(request)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Consolidated processing failed: {e}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

@app.post("/fix-error", response_model=ConsolidatedFixResponse)
async def fix_single_error(request: ErrorRequest):
    """Single error endpoint"""
    try:
        batch_request = BatchErrorRequest(
            errors=[
                BatchErrorItem(
                    error_message=request.error_message,
                    code_snippet=request.code_snippet,
                    line_number=request.line_number,
                    error_id=f"single_{int(time.time() * 1000)}"
                )
            ],
            file_path=request.file_path
        )
        
        result = await fix_errors_consolidated(batch_request)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Single error processing failed: {str(e)}")

@app.get("/cache/stats")
async def get_cache_stats():
    return {"cache_stats": fix_cache.stats()}

@app.post("/cache/clear")
async def clear_cache():
    global fix_cache
    fix_cache = FixCache(max_size=1000)
    return {"message": "Cache cleared successfully"}

@app.get("/")
async def root():
    return {
        "message": "Consolidated AI Error Fixer API",
        "version": "5.0.0",
        "features": [
            "Processes all errors at once",
            "Clean code output without line numbers",
            "Ready-to-paste fixes",
            "Primary and alternative solutions",
            "Consolidated explanations"
        ],
        "status": "ready"
    }

if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Consolidated AI Error Fixer API...")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
