import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import org.apache.commons.cli.CommandLine;
import org.obolibrary.robot.CommandLineHelper;
import org.obolibrary.robot.CommandState;
import org.obolibrary.robot.IOHelper;
import org.obolibrary.robot.ReasonCommand;
import org.obolibrary.robot.ReasonOperation;
import org.semanticweb.HermiT.Configuration;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.reasoner.OWLReasoner;
import org.slf4j.LoggerFactory;

/**
 * Equivalent of `robot reason --reasoner hermit`, but with HermiT's existential expansion
 * strategy set to INDIVIDUAL_REUSE instead of the default CREATION_ORDER (EN-14214). The
 * HERMIT_STRATEGY environment variable overrides it; CREATION_ORDER gives stock HermiT.
 *
 * <p>Nearly all of HermiT's time on this ontology goes to building a tableau model per class
 * during classification; reusing individuals makes that 4-6x faster, with output
 * byte-identical to stock HermiT. ROBOT's CLI has no way to set this, so this drives ROBOT's
 * own ReasonOperation as a library with a tuned reasoner factory. Redundancy removal, the
 * equivalent-classes policy, unsatisfiable-class checks and axiom generators all behave
 * exactly as in `robot reason`.
 *
 * <p>Run with Java's single-file source launcher against the ROBOT jar (no build step):
 *
 * <pre>
 *   java --class-path robot.jar src/java/ReasonReuse.java IN.owl OUT.owl [robot reason options]
 * </pre>
 *
 * The options are `robot reason`'s own, e.g. --axiom-generators "SubClass ClassAssertion",
 * except --input, --output and --reasoner, which this program supplies; unspecified options
 * take ROBOT's defaults.
 *
 * <p>Individual reuse with backtracking is proven sound and complete (Motik and Horrocks,
 * "Individual Reuse in Description Logic Reasoning", IJCAR 2008, Theorem 1), so it changes
 * only the time taken. The EL strategy in the same enum reuses without backtracking, which is
 * guaranteed complete only for EL ontologies; it yields spurious unsatisfiable classes on
 * sir-ont.owl. Compare against stock HermiT (HERMIT_STRATEGY=CREATION_ORDER) when upgrading
 * ROBOT.
 */
public class ReasonReuse {
  public static void main(String[] args) throws Exception {
    if (args.length < 2) {
      System.err.println("Usage: ReasonReuse IN.owl OUT.owl [robot reason options]");
      System.exit(2);
    }
    String[] reasonArgs = Arrays.copyOfRange(args, 2, args.length);
    ReasonCommand command = new ReasonCommand();
    CommandLine line;
    Configuration.ExistentialStrategyType strategy;
    try {
      String strategyName = System.getenv().getOrDefault("HERMIT_STRATEGY", "INDIVIDUAL_REUSE");
      strategy =
          Arrays.stream(Configuration.ExistentialStrategyType.values())
              .filter(s -> s.name().equals(strategyName))
              .findFirst()
              .orElseThrow(
                  () ->
                      new IllegalArgumentException(
                          "HERMIT_STRATEGY must be one of "
                              + Arrays.toString(Configuration.ExistentialStrategyType.values()) + ", not '"
                              + strategyName + "'"));
      line = CommandLineHelper.getCommandLine(command.getUsage(), command.getOptions(), reasonArgs);
      if (!line.getArgList().isEmpty()) {
        throw new IllegalArgumentException("unexpected argument(s) " + line.getArgList());
      }
      for (String supplied : List.of("input", "output", "reasoner")) {
        if (line.hasOption(supplied)) {
          throw new IllegalArgumentException("--" + supplied + " is supplied by ReasonReuse");
        }
      }
      // ROBOT validates option values inline in ReasonCommand.execute, so reuse its checks by
      // running it on an empty ontology, which takes no time.
      CommandState empty = new CommandState();
      empty.setOntology(OWLManager.createOWLOntologyManager().createOntology());
      command.execute(empty, reasonArgs);
    } catch (Exception e) {
      System.err.println("ReasonReuse: " + e.getMessage());
      System.exit(2);
      return;
    }

    // Same option mapping as ReasonCommand.execute.
    Map<String, String> options = ReasonOperation.getDefaultOptions();
    for (String option : options.keySet()) {
      if (line.hasOption(option)) {
        options.put(option, line.getOptionValue(option));
      }
    }

    // Match `robot reason -vv`, which is how ROBOT itself sets the log level.
    Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
    root.setLevel(Level.INFO);

    IOHelper ioHelper = new IOHelper();
    OWLOntology ontology = ioHelper.loadOntology(args[0]);
    ReasonOperation.reason(ontology, new TunedHermiT(strategy), options);
    ioHelper.saveOntology(ontology, args[1]);
  }

  /** Stock HermiT, except for the existential expansion strategy. */
  static class TunedHermiT extends org.semanticweb.HermiT.ReasonerFactory {
    private final Configuration.ExistentialStrategyType strategy;

    TunedHermiT(Configuration.ExistentialStrategyType strategy) {
      this.strategy = strategy;
    }

    @Override
    public OWLReasoner createReasoner(OWLOntology ontology) {
      Configuration config = getProtegeConfiguration(null);  // stock defaults, as robot reason uses
      config.existentialStrategyType = strategy;
      // createHermiTOWLReasoner is protected, hence the subclass.
      return createHermiTOWLReasoner(config, ontology);
    }
  }
}
