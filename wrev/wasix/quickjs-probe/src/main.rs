use std::{future::poll_fn, task::Poll};

use rquickjs::{
    AsyncContext, AsyncRuntime, CatchResultExt, Object, Promise,
    function::{Async, Func},
};

async fn call_tool(name: String) -> rquickjs::Result<i32> {
    // Force the Rust future to yield before settling the JavaScript promise.
    let mut yielded = false;
    poll_fn(|cx| {
        if yielded {
            Poll::Ready(())
        } else {
            yielded = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        }
    })
    .await;

    match name.as_str() {
        "first" => Ok(19),
        "second" => Ok(23),
        _ => Err(rquickjs::Error::new_from_js_message(
            "string",
            "tool",
            "unknown mock tool",
        )),
    }
}

fn main() {
    futures::executor::block_on(async {
        let runtime = AsyncRuntime::new().expect("initialize QuickJS");
        let context = AsyncContext::full(&runtime).await.expect("create context");
        context
            .async_with(async |ctx| {
                assert_eq!(ctx.eval::<i32, _>("1 + 1").catch(&ctx).unwrap(), 2);
                assert!(
                    ctx.eval::<bool, _>("Date.now() > 1700000000000")
                        .catch(&ctx)
                        .unwrap()
                );

                ctx.globals()
                    .set("rustAddOne", Func::from(|value: i32| value + 1))
                    .unwrap();
                assert_eq!(
                    ctx.eval::<i32, _>("rustAddOne(41)").catch(&ctx).unwrap(),
                    42
                );

                let mcp = Object::new(ctx.clone()).unwrap();
                mcp.set("listTools", Func::from(|| vec!["first", "second"]))
                    .unwrap();
                mcp.set("callTool", Func::from(Async(call_tool))).unwrap();
                ctx.globals().set("mcp", mcp).unwrap();

                let script = r#"
                (async () => {
                    const tools = mcp.listTools();
                    if (tools.join(",") !== "first,second") throw new Error("Unexpected tools");
                    return (await mcp.callTool(tools[0])) + (await mcp.callTool(tools[1]));
                })()
            "#;
                let promise = ctx.eval::<Promise, _>(script).catch(&ctx).unwrap();
                assert_eq!(promise.into_future::<i32>().await.catch(&ctx).unwrap(), 42);
            })
            .await;
    });
    println!("PASS: QuickJS evaluation, Rust callback, and two awaited mock MCP tool calls");
}
