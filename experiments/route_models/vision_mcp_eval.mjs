// Development-only evaluation through the unmodified official vision MCP server.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const modules=path.join(root,'data/glm-coding-mcp/node_modules');
const {Client}=await import(path.join(modules,'@modelcontextprotocol/sdk/dist/esm/client/index.js'));
const {StdioClientTransport}=await import(path.join(modules,'@modelcontextprotocol/sdk/dist/esm/client/stdio.js'));
const [imagePath,promptPath,outputPath]=process.argv.slice(2);
if (!outputPath) throw new Error('Usage: vision_mcp_eval.mjs IMAGE PROMPT_FILE OUTPUT_JSON');
const key=fs.readFileSync(path.join(root,'glm/apikey.txt'),'utf8').trim();
const transport=new StdioClientTransport({command:process.execPath,
  args:[path.join(modules,'@z_ai/mcp-server/build/index.js')],stderr:'ignore',
  env:{...process.env,Z_AI_API_KEY:key,Z_AI_MODE:'ZHIPU',Z_AI_VISION_MODEL:'glm-5.3-flash',
    Z_AI_VISION_MODEL_MAX_TOKENS:'8192',Z_AI_TIMEOUT:'90000',Z_AI_RETRY_COUNT:'0'}});
const client=new Client({name:'wow-navigation-development-evaluation',version:'1.0'});
const begin=Date.now();
const row={transport:'official_vision_mcp',model:'glm-5.3-flash',execution_authorized:false,
  image:path.resolve(imagePath),prompt:fs.readFileSync(promptPath,'utf8')};
try {
  await client.connect(transport);
  row.result=await client.callTool({name:'analyze_image',arguments:{image_source:row.image,prompt:row.prompt}},undefined,{timeout:110000});
} catch(error) {row.error=String(error.message).split(key).join('[REDACTED]');}
finally {
  await client.close();row.elapsed_s=(Date.now()-begin)/1000;
  fs.mkdirSync(path.dirname(outputPath),{recursive:true});
  fs.writeFileSync(outputPath,JSON.stringify(row,null,2).split(key).join('[REDACTED]'));
  console.log(JSON.stringify({output:outputPath,elapsed_s:row.elapsed_s,error:row.error,isError:row.result?.isError}));
}
