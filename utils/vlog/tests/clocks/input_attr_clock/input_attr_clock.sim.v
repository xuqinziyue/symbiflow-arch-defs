/*
 * `input wire a` should be detected as a clock because of the `(* CLOCK *)`
 * attribute.
 */
(* whitebox *)
module block(a, b, o);
	(* CLOCK *)
	input wire a;
	input wire b;
	output wire o;
endmodule
